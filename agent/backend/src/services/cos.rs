//! 腾讯云 COS 对象存储备份上传（免 SDK：手写 XML API 签名，reqwest 直连）。
//!
//! 对应 Python 版 app/services/cos_service.py：
//!   - 配置存 cos_config（单行，id=1）
//!   - 上传使用简单 PUT（单对象上限 5GB，备份文件足够），流式读文件不占大内存
//!   - 凭证校验用 GET bucket（?max-keys=1）
//!
//! 签名说明：COS XML API 的 Authorization 使用 HMAC-SHA1（q-sign-algorithm=sha1），
//! 而项目依赖里只有 sha2（SHA-256），因此本文件内置一份精简 SHA-1 与 HMAC-SHA1 实现，
//! 保证与 Python（hashlib.sha1）逐字节一致；HMAC 的拼接规则与 hmac 库等价。

use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use sqlx::SqlitePool;
use tokio_util::io::ReaderStream;

use crate::error::AppResult;
use crate::models::CosConfig;

fn client(timeout: Duration) -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(timeout)
        .build()
        .unwrap_or_default()
}

// ---------- SHA-1 / HMAC-SHA1（COS 签名专用，仅内部使用） ----------

fn sha1(data: &[u8]) -> [u8; 20] {
    let mut h: [u32; 5] = [
        0x6745_2301,
        0xEFCD_AB89,
        0x98BA_DCFE,
        0x1032_5476,
        0xC3D2_E1F0,
    ];
    let mut msg = data.to_vec();
    let bit_len = (data.len() as u64) * 8;
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());
    for chunk in msg.chunks(64) {
        let mut w = [0u32; 80];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                chunk[4 * i],
                chunk[4 * i + 1],
                chunk[4 * i + 2],
                chunk[4 * i + 3],
            ]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A82_7999u32),
                20..=39 => (b ^ c ^ d, 0x6ED9_EBA1u32),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1B_BCDCu32),
                _ => (b ^ c ^ d, 0xCA62_C1D6u32),
            };
            let tmp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(*wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = tmp;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }
    let mut out = [0u8; 20];
    for (i, v) in h.iter().enumerate() {
        out[4 * i..4 * i + 4].copy_from_slice(&v.to_be_bytes());
    }
    out
}

fn hmac_sha1(key: &[u8], msg: &[u8]) -> [u8; 20] {
    let mut k = key.to_vec();
    if k.len() > 64 {
        k = sha1(&k).to_vec();
    }
    k.resize(64, 0);
    let mut ipad = vec![0x36u8; 64];
    let mut opad = vec![0x5cu8; 64];
    for i in 0..64 {
        ipad[i] ^= k[i];
        opad[i] ^= k[i];
    }
    let mut inner = ipad;
    inner.extend_from_slice(msg);
    let inner_hash = sha1(&inner);
    let mut outer = opad;
    outer.extend_from_slice(&inner_hash);
    sha1(&outer)
}

fn hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

// ---------- 配置 ----------

/// 读取 COS 配置行（无则建空行 id=1）。
pub async fn get_config(pool: &SqlitePool) -> AppResult<CosConfig> {
    if let Some(row) = sqlx::query_as::<_, CosConfig>("SELECT * FROM cos_config WHERE id = 1")
        .fetch_optional(pool)
        .await?
    {
        return Ok(row);
    }
    sqlx::query(
        "INSERT INTO cos_config (id, secret_id, secret_key, bucket, region, prefix, keep_local, enabled, updated_at) \
         VALUES (1, '', '', '', '', 'ppanel-backups', 1, 0, ?)",
    )
    .bind(crate::models::utcnow())
    .execute(pool)
    .await?;
    Ok(sqlx::query_as::<_, CosConfig>("SELECT * FROM cos_config WHERE id = 1")
        .fetch_one(pool)
        .await?)
}

/// secret_key 掩码（与 Python get_cos 一致）：>8 位保留首尾 4 位，否则 ****。
pub fn masked_key(key: &str) -> String {
    if key.len() > 8 {
        let chars: Vec<char> = key.chars().collect();
        let head: String = chars[..4].iter().collect();
        let tail: String = chars[chars.len() - 4..].iter().collect();
        format!("{head}****{tail}")
    } else if key.is_empty() {
        String::new()
    } else {
        "****".to_string()
    }
}

// ---------- 签名 ----------

fn sign(secret_key: &str, key_time: &str, method: &str, path: &str) -> String {
    let sign_key = hex(&hmac_sha1(secret_key.as_bytes(), key_time.as_bytes()));
    // StringToSign: HttpMethod\nUriPathname\nHttpParams\nHttpHeaders\n（后两者此处为空）
    let string_to_sign = format!("{}\n{}\n\n\n", method.to_lowercase(), path);
    hex(&hmac_sha1(sign_key.as_bytes(), string_to_sign.as_bytes()))
}

fn auth_header(cfg: &CosConfig, method: &str, path: &str) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let key_time = format!("{};{}", now - 60, now + 600);
    let sig = sign(&cfg.secret_key, &key_time, method, path);
    format!(
        "q-sign-algorithm=sha1&q-ak={}&q-sign-time={key_time}\
         &q-key-time={key_time}&q-header-list=&q-url-param-list=&q-signature={sig}",
        cfg.secret_id
    )
}

fn base_url(cfg: &CosConfig) -> String {
    format!("https://{}.cos.{}.myqcloud.com", cfg.bucket, cfg.region)
}

fn prefix_of(cfg: &CosConfig) -> String {
    let p = cfg.prefix.trim_matches('/');
    if p.is_empty() {
        "ppanel-backups".to_string()
    } else {
        p.to_string()
    }
}

fn brief(text: &str) -> String {
    text.chars().take(160).collect()
}

// ---------- 连通性校验 ----------

/// 校验凭证与桶可访问性（GET bucket ?max-keys=1）。失败返回给用户看的文案。
pub async fn test_conn(cfg: &CosConfig) -> Result<Value, String> {
    if cfg.secret_id.is_empty()
        || cfg.secret_key.is_empty()
        || cfg.bucket.is_empty()
        || cfg.region.is_empty()
    {
        return Err("请先填写 SecretId / SecretKey / 存储桶 / 地域".to_string());
    }
    let url = format!("{}/?max-keys=1", base_url(cfg));
    let resp = client(Duration::from_secs(10))
        .get(&url)
        .header("Authorization", auth_header(cfg, "get", "/"))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status().as_u16();
    if status == 200 {
        return Ok(json!({"ok": true, "message": "连接成功，桶可访问"}));
    }
    let text = resp.text().await.unwrap_or_default();
    let msg = match status {
        403 => "连接被拒（403）：SecretId/SecretKey 无该桶权限或防盗链限制".to_string(),
        404 => "存储桶不存在（404）：检查桶名与地域是否匹配".to_string(),
        _ => format!("COS 返回 {status}：{}", brief(&text)),
    };
    Err(msg)
}

// ---------- 上传 / 删除 / 列举 / 清理 ----------

/// 上传备份文件到 COS（流式读盘），返回对象 key 与大小。
pub async fn upload(
    cfg: &CosConfig,
    file_path: &str,
    remote_name: Option<&str>,
) -> Result<Value, String> {
    let size = std::fs::metadata(file_path)
        .map_err(|e| format!("读取备份文件失败：{e}"))?
        .len();
    let name = remote_name
        .map(|s| s.to_string())
        .unwrap_or_else(|| {
            Path::new(file_path)
                .file_name()
                .map(|s| s.to_string_lossy().to_string())
                .unwrap_or_default()
        });
    let key = format!("{}/{}", prefix_of(cfg), name);
    let path = format!("/{key}");
    let url = format!("{}{path}", base_url(cfg));
    let file = tokio::fs::File::open(file_path)
        .await
        .map_err(|e| format!("读取备份文件失败：{e}"))?;
    let body = reqwest::Body::wrap_stream(ReaderStream::new(file));
    let resp = client(Duration::from_secs(600))
        .put(&url)
        .header("Authorization", auth_header(cfg, "put", &path))
        .header("Content-Length", size.to_string())
        .body(body)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status().as_u16();
    if status != 200 {
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("COS 上传失败（{status}）：{}", brief(&text)));
    }
    Ok(json!({"key": key, "size": size}))
}

/// 删除 COS 对象（静默容忍 404）。
pub async fn remove(cfg: &CosConfig, remote_name: &str) -> Result<(), String> {
    let key = format!("{}/{}", prefix_of(cfg), remote_name);
    let path = format!("/{key}");
    let url = format!("{}{path}", base_url(cfg));
    let resp = client(Duration::from_secs(30))
        .delete(&url)
        .header("Authorization", auth_header(cfg, "delete", &path))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status().as_u16();
    if status != 200 && status != 204 && status != 404 {
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("COS 删除失败（{status}）：{}", brief(&text)));
    }
    Ok(())
}

/// 列举当前前缀下的对象 key（GET bucket ?prefix=&max-keys=）。
pub async fn list_objects(cfg: &CosConfig, max_keys: i64) -> Result<Vec<String>, String> {
    let url = format!(
        "{}/?prefix={}&max-keys={}",
        base_url(cfg),
        urlencoding::encode(&format!("{}/", prefix_of(cfg))),
        max_keys.max(1)
    );
    let resp = client(Duration::from_secs(30))
        .get(&url)
        .header("Authorization", auth_header(cfg, "get", "/"))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status().as_u16();
    let text = resp.text().await.unwrap_or_default();
    if status != 200 {
        return Err(format!("COS 列举失败（{status}）：{}", brief(&text)));
    }
    let mut keys = Vec::new();
    let mut rest = text.as_str();
    while let Some(start) = rest.find("<Key>") {
        let after = &rest[start + 5..];
        let Some(end) = after.find("</Key>") else { break };
        keys.push(after[..end].to_string());
        rest = &after[end + 6..];
    }
    Ok(keys)
}

/// 远端按保留份数清理（key 名含时间戳，按字典序即时间序）：返回删除数量。
pub async fn cleanup_remote(cfg: &CosConfig, keep: i64) -> Result<i64, String> {
    let keep = keep.max(1) as usize;
    let mut keys = list_objects(cfg, 1000).await?;
    keys.sort();
    let mut removed = 0i64;
    let total = keys.len();
    if total > keep {
        for k in &keys[..total - keep] {
            let name = k.rsplit('/').next().unwrap_or(k).to_string();
            if remove(cfg, &name).await.is_ok() {
                removed += 1;
            }
        }
    }
    Ok(removed)
}
