//! 运行时共享状态：DB 池、Docker、执行任务注册表、任务日志、升级状态、域名缓存。

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use sqlx::SqlitePool;

use crate::docker::DockerHandle;

#[derive(Clone)]
pub struct AppState {
    pub db: SqlitePool,
    pub docker: Arc<DockerHandle>,
    pub jobs: Arc<JobRegistry>,
    pub tasks: Arc<TaskLog>,
    pub upgrade: Arc<UpgradeState>,
    pub domains: Arc<DomainCache>,
}

impl AppState {
    pub fn new(db: SqlitePool) -> Self {
        Self {
            db,
            docker: DockerHandle::new(),
            jobs: Arc::new(JobRegistry::default()),
            tasks: Arc::new(TaskLog::default()),
            upgrade: Arc::new(UpgradeState::default()),
            domains: Arc::new(DomainCache::default()),
        }
    }
}

// ---------- 容器内执行任务（依赖安装/运行环境切换等，内存态） ----------

pub struct ExecJob {
    pub id: String,
    pub instance_id: i64,
    pub cmd: Vec<String>,
    pub desc: String,
    pub lines: Mutex<VecDeque<String>>,
    pub done: AtomicBool,
    pub exit_code: Mutex<Option<i64>>,
}

impl ExecJob {
    pub fn new(instance_id: i64, cmd: Vec<String>, desc: impl Into<String>) -> Arc<Self> {
        Arc::new(Self {
            id: uuid::Uuid::new_v4().simple().to_string(),
            instance_id,
            cmd,
            desc: desc.into(),
            lines: Mutex::new(VecDeque::with_capacity(8000)),
            done: AtomicBool::new(false),
            exit_code: Mutex::new(None),
        })
    }

    pub fn emit(&self, text: impl Into<String>) {
        let mut lines = self.lines.lock().unwrap();
        if lines.len() >= 8000 {
            lines.pop_front();
        }
        lines.push_back(text.into());
    }

    pub fn snapshot(&self) -> Vec<String> {
        self.lines.lock().unwrap().iter().cloned().collect()
    }

    pub fn is_done(&self) -> bool {
        self.done.load(Ordering::SeqCst)
    }

    pub fn finish(&self, code: i64) {
        *self.exit_code.lock().unwrap() = Some(code);
        self.done.store(true, Ordering::SeqCst);
    }
}

#[derive(Default)]
pub struct JobRegistry {
    jobs: Mutex<HashMap<String, Arc<ExecJob>>>,
}

impl JobRegistry {
    pub fn insert(&self, job: Arc<ExecJob>) -> Arc<ExecJob> {
        self.jobs
            .lock()
            .unwrap()
            .insert(job.id.clone(), job.clone());
        job
    }

    pub fn get(&self, id: &str) -> Option<Arc<ExecJob>> {
        self.jobs.lock().unwrap().get(id).cloned()
    }

    /// 清理同实例已完成的历史任务，保持注册表精简。
    pub fn cleanup_finished(&self, instance_id: i64) {
        self.jobs
            .lock()
            .unwrap()
            .retain(|_, j| !(j.instance_id == instance_id && j.is_done()));
    }

    /// 同实例同时只允许一个执行任务（防 pip/pecl 并发互踩）。
    pub fn has_running(&self, instance_id: i64) -> bool {
        self.jobs
            .lock()
            .unwrap()
            .values()
            .any(|j| j.instance_id == instance_id && !j.is_done())
    }
}

// ---------- 耗时任务步骤日志（X-Task-Id 配合 /agent/host/tasks/lines） ----------

#[derive(Default)]
pub struct TaskLog {
    inner: Mutex<TaskInner>,
}

#[derive(Default)]
struct TaskInner {
    tasks: HashMap<String, TaskEntry>,
    order: VecDeque<String>,
}

pub struct TaskEntry {
    pub title: String,
    pub status: String,
    pub lines: Vec<String>,
}

const TASK_KEEP: usize = 30;
const TASK_MAX_LINES: usize = 400;

impl TaskLog {
    pub fn start(&self, task_id: &str, title: &str) {
        if task_id.is_empty() {
            return;
        }
        let mut g = self.inner.lock().unwrap();
        g.tasks.remove(task_id);
        while g.order.len() >= TASK_KEEP {
            if let Some(old) = g.order.pop_front() {
                g.tasks.remove(&old);
            }
        }
        g.tasks.insert(
            task_id.to_string(),
            TaskEntry {
                title: title.to_string(),
                status: "running".to_string(),
                lines: Vec::new(),
            },
        );
        g.order.push_back(task_id.to_string());
    }

    pub fn log(&self, task_id: &str, line: &str) {
        if task_id.is_empty() || line.is_empty() {
            return;
        }
        let mut g = self.inner.lock().unwrap();
        if let Some(t) = g.tasks.get_mut(task_id) {
            let stamp = chrono::Local::now().format("%H:%M:%S").to_string();
            t.lines.push(format!("[{stamp}] {line}"));
            if t.lines.len() > TASK_MAX_LINES {
                let cut = t.lines.len() - TASK_MAX_LINES;
                t.lines.drain(0..cut);
            }
        }
    }

    /// 取任务的全部日志行（供定时任务把过程日志回写进 last_output）。
    pub fn all_lines(&self, task_id: &str) -> Vec<String> {
        let g = self.inner.lock().unwrap();
        g.tasks
            .get(task_id)
            .map(|t| t.lines.clone())
            .unwrap_or_default()
    }

    pub fn finish(&self, task_id: &str, ok: bool, msg: &str) {
        let mut g = self.inner.lock().unwrap();
        if let Some(t) = g.tasks.get_mut(task_id) {
            t.status = if ok { "done".into() } else { "error".into() };
            if !msg.is_empty() {
                let stamp = chrono::Local::now().format("%H:%M:%S").to_string();
                t.lines.push(format!("[{stamp}] {msg}"));
            }
        }
    }

    pub fn lines(&self, task_id: &str, since: usize) -> serde_json::Value {
        let g = self.inner.lock().unwrap();
        match g.tasks.get(task_id) {
            None => serde_json::json!({"lines": [], "total": 0, "status": ""}),
            Some(t) => {
                let from = since.min(t.lines.len());
                serde_json::json!({
                    "lines": t.lines[from..],
                    "total": t.lines.len(),
                    "status": t.status,
                })
            }
        }
    }
}

// ---------- 在线升级状态 ----------

#[derive(Default)]
pub struct UpgradeState {
    pub inner: Mutex<UpgradeInner>,
}

#[derive(Default)]
pub struct UpgradeInner {
    pub running: bool,
    pub stage: String,
    pub log: Vec<String>,
    pub ok: Option<bool>,
    pub error: String,
    pub started_at: f64,
    pub finished_at: f64,
}

impl UpgradeState {
    pub fn status_json(&self) -> serde_json::Value {
        let g = self.inner.lock().unwrap();
        serde_json::json!({
            "running": g.running,
            "stage": g.stage,
            "log": g.log,
            "ok": g.ok,
            "error": g.error,
            "started_at": g.started_at,
            "finished_at": g.finished_at,
        })
    }

    pub fn log(&self, msg: &str) {
        let mut g = self.inner.lock().unwrap();
        g.log.push(msg.to_string());
        if g.log.len() > 200 {
            let cut = g.log.len() - 200;
            g.log.drain(0..cut);
        }
    }
}

// ---------- 域名 → 端口缓存（20s） ----------

#[derive(Default)]
pub struct DomainCache {
    inner: tokio::sync::RwLock<(Option<Instant>, HashMap<String, i64>)>,
}

impl DomainCache {
    /// 读取缓存快照：`Some(map)` = 仍在有效期（可能为空表），`None` = 已过期需回源。
    pub async fn fresh(&self) -> Option<HashMap<String, i64>> {
        let g = self.inner.read().await;
        match g.0 {
            Some(at) if at.elapsed().as_secs_f64() < 20.0 => Some(g.1.clone()),
            _ => None,
        }
    }

    /// 覆盖缓存（回源后写入）。
    pub async fn store(&self, map: HashMap<String, i64>) {
        let mut g = self.inner.write().await;
        g.0 = Some(Instant::now());
        g.1 = map;
    }

    pub async fn get(&self, host: &str) -> Option<i64> {
        let (at, map) = {
            let g = self.inner.read().await;
            (g.0, g.1.clone())
        };
        if let Some(at) = at {
            if at.elapsed().as_secs_f64() < 20.0 {
                return map.get(host).copied();
            }
        }
        None
    }

    /// 返回 (是否需刷新, 命中端口)。
    pub async fn lookup(&self, host: &str, refresh: impl FnOnce() -> (Instant, HashMap<String, i64>)) -> Option<i64> {
        let (at, map) = {
            let g = self.inner.read().await;
            (g.0, g.1.clone())
        };
        if let Some(at) = at {
            if at.elapsed().as_secs_f64() < 20.0 {
                return map.get(host).copied();
            }
        }
        let (new_at, new_map) = refresh();
        let hit = new_map.get(host).copied();
        {
            let mut g = self.inner.write().await;
            g.0 = Some(new_at);
            g.1 = new_map;
        }
        hit
    }
}
