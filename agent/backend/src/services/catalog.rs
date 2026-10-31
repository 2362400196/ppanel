//! 面板静态目录数据（PHP 扩展 / pip 源 / npm 源 / Go 代理 / 常用依赖清单）。
//!
//! 数据由脚本 `scripts/gen_catalog.py` 从 Python 版路由中提取生成到 catalog.json，
//! 编译期 include_str! 进来、首次使用时解析为 JSON，运行时零 IO。

use std::sync::OnceLock;

use serde_json::Value;

const RAW: &str = include_str!("catalog.json");

fn data() -> &'static Value {
    static D: OnceLock<Value> = OnceLock::new();
    D.get_or_init(|| serde_json::from_str(RAW).unwrap_or(Value::Null))
}

fn list(key: &str) -> Vec<Value> {
    data()
        .get(key)
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default()
}

/// Python 常用依赖目录（面板一键安装）。
pub fn py_pkgs() -> Vec<Value> {
    list("py_pkgs")
}

/// pip 镜像源（国内加速）。
pub fn py_mirrors() -> Vec<Value> {
    list("py_mirrors")
}

/// PHP 扩展目录（builtin / core / pecl / loader 四类）。
pub fn php_ext() -> Vec<Value> {
    list("php_ext")
}

/// PHP 常见禁用函数候选。
pub fn php_funcs() -> Vec<Value> {
    list("php_funcs")
}

/// Node 常用依赖目录。
pub fn node_pkgs() -> Vec<Value> {
    list("node_pkgs")
}

/// npm registry 镜像源。
pub fn npm_mirrors() -> Vec<Value> {
    list("npm_mirrors")
}

/// GOPROXY 模块代理。
pub fn go_proxies() -> Vec<Value> {
    list("go_proxies")
}

/// 目录中按 name 找一项（PHP 扩展安装/卸载用）。
pub fn php_ext_by_name(name: &str) -> Option<Value> {
    php_ext()
        .into_iter()
        .find(|x| x.get("name").and_then(|v| v.as_str()) == Some(name))
}

/// 镜像源 url → 展示名（找不到则回退 url 本身）。
pub fn mirror_name(mirrors: Vec<Value>, url: &str) -> String {
    mirrors
        .into_iter()
        .find(|m| m.get("url").and_then(|v| v.as_str()) == Some(url))
        .and_then(|m| m.get("name").and_then(|v| v.as_str()).map(|s| s.to_string()))
        .unwrap_or_else(|| url.to_string())
}
