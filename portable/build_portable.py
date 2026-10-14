"""AIMC Windows 便携版打包脚本 (维护者用, 在有 Python 3.11+ 与 git 的 Windows 上运行)

产出: dist/AIMC-Portable-v<version>.zip — 解压后双击 start-aimc.bat 即用
(内嵌 Python 3.12 + 预装依赖, 首次启动零网络)。

用法 (仓库根目录):
    python portable/build_portable.py                     # 打包当前工作树 (git 追踪文件)
    python portable/build_portable.py --source head       # 打包 HEAD 已提交内容
    python portable/build_portable.py --skip-deps         # 跳过依赖预装 (产出小, 首启需联网)
    python portable/build_portable.py --index-url <url>   # 自定义 pip 镜像

步骤:
1. 下载 Python 3.12 embeddable zip (curl, npmmirror / huaweicloud / python.org 依次回退)
2. 解压 + 启用 site (改 python312._pth)
3. 用本机 pip 下载 pip wheel → 离线引导进内嵌 Python (不依赖 get-pip.py)
4. pip install -r requirements.txt (可选 --skip-deps)
5. git archive / git ls-files 复制源码 (永远排除 .env / data/ / tests 等)
6. 拷贝启动脚本 + 生成带 BOM 的使用说明 (记事本不乱码)
7. 依赖导入自检 + 打 zip
"""
import argparse
import ipaddress
import io
import shutil
import socket
import subprocess
import sys
import tarfile
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = REPO_ROOT / "portable" / "AIMC-Portable"
DIST_DIR = REPO_ROOT / "dist"

PY_EMBED_VERSION = "3.12.10"
PY_EMBED_NAME = f"python-{PY_EMBED_VERSION}-embed-amd64.zip"
# 下载源白名单: 仅 https + 固定镜像域名 + 固定路径前缀 (构建期常量)
PY_EMBED_MIRRORS = [
    f"https://registry.npmmirror.com/-/binary/python/{PY_EMBED_VERSION}/{PY_EMBED_NAME}",
    f"https://mirrors.huaweicloud.com/python/{PY_EMBED_VERSION}/{PY_EMBED_NAME}",
    f"https://www.python.org/ftp/python/{PY_EMBED_VERSION}/{PY_EMBED_NAME}",
]
DOWNLOAD_HOSTS = {
    "registry.npmmirror.com": "/-/binary/python/",
    "mirrors.huaweicloud.com": "/python/",
    "www.python.org": "/ftp/python/",
}
DEFAULT_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"

USAGE_TXT = """AIMC Portable - AI Model Compass 便携版
========================================

【快速开始】
1. 双击 start-aimc.bat
2. 浏览器自动打开看板 (http://127.0.0.1:18080/dashboard/)
3. 首次启动会在看板里进入「初始化配置」页 —— 按引导注册渠道并填入 API Key
4. 客户端调用: base_url=http://127.0.0.1:18080/v1
   api_key 见 .env 文件里的 AIMC_API_KEY (首次启动自动随机生成)

【停止】
关闭 start-aimc.bat 的命令行窗口, 或双击 stop-aimc.bat。

【文件位置】
- 配置: .env (首次启动自动生成; 含加密主密钥 AIMC_MASTER_KEY, 请备份)
- 数据库: data/aimc.db (所有用量/渠道/情报数据都在这一个文件里)
- 备份 = 复制 data/ + .env 两个文件即可

【安全说明】
- 服务只绑定 127.0.0.1, 局域网其他设备无法访问
- .env 里 AIMC_MASTER_KEY 丢失 = 已录入的渠道 key 全部无法解密

【已知限制 (与 Linux 部署版差异)】
- 不含 Playwright 浏览器内核: 网页型情报采集源不可用 (模型价格等
  httpx 型采集源不受影响); 需要完整采集请用 Linux 部署版
- 升级方式: 重新下载新便携版, 用旧包的 data/ + .env 覆盖新包同名文件
"""


def _assert_public_https_host(url: str, host_prefixes: dict | None = None) -> None:
    """下载源校验: https + (可选)白名单域名/路径 + 解析 IP 必须是公网地址"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise SystemExit(f"拒绝非 https 下载源: {url}")
    host = parsed.hostname or ""
    if host_prefixes is not None:
        allowed_prefix = host_prefixes.get(host)
        if allowed_prefix is None or not parsed.path.startswith(allowed_prefix):
            raise SystemExit(f"下载源不在白名单: {url}")
    for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise SystemExit(f"域名解析到非公网地址, 拒绝: {host} -> {ip}")


def _download_via_curl(mirror: str, embed_zip: Path) -> None:
    """用系统 curl 下载 (Win10 1803+ 自带); 源已过白名单+公网 IP 校验"""
    _assert_public_https_host(mirror)
    print(f"  下载 {mirror}")
    subprocess.run(
        ["curl", "--fail", "--location", "--silent", "--show-error",
         "--max-time", "300", "--output", str(embed_zip), mirror],
        check=True, shell=False, cwd=str(REPO_ROOT))


def _read_version() -> str:
    for line in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


def _zip_members_safe(names: list) -> None:
    """防 zip-slip: 拒绝绝对路径 / 盘符 / .. 成员 (纯字符串检查)"""
    for member in names:
        parts = PurePosixPath(member.replace("\\", "/")).parts
        if member.startswith(("/", "\\")) or ".." in parts:
            raise SystemExit(f"zip 内含越界路径成员, 拒绝解压: {member}")
        if len(parts) > 0 and len(parts[0]) == 2 and parts[0][1] == ":":
            raise SystemExit(f"zip 内含盘符路径成员, 拒绝解压: {member}")


def _log(cmd: list) -> None:
    print("  $ " + " ".join(str(c) for c in cmd))


def setup_embedded_python(emb_dir: Path, skip_deps: bool, index_url: str) -> None:
    py_exe = emb_dir / "python.exe"
    zip_cache = REPO_ROOT / "portable" / "build"
    zip_cache.mkdir(parents=True, exist_ok=True)
    embed_zip = zip_cache / PY_EMBED_NAME

    if not embed_zip.exists():
        last_err = None
        for mirror in PY_EMBED_MIRRORS:
            try:
                _download_via_curl(mirror, embed_zip)
                break
            except Exception as e:  # noqa: BLE001 - 逐镜像回退
                last_err = e
                print(f"  回退下一镜像 ({e})")
        else:
            raise SystemExit(f"Python embeddable 下载失败: {last_err}")
    print(f"  解压 {embed_zip.name} -> {emb_dir.name}/")
    with zipfile.ZipFile(embed_zip) as zf:
        _zip_members_safe(zf.namelist())
        zf.extractall(str(emb_dir))

    # 启用 site → site-packages 可导入; 并把包根目录 (python/ 的上一级) 加进
    # sys.path — ._pth 存在时 Python 走隔离路径配置, 脚本目录不会自动加入,
    # 不补这条 `from gateway.app import ...` 会 ModuleNotFoundError
    pth = next(emb_dir.glob("python*._pth"))
    lines = pth.read_text(encoding="utf-8").splitlines()
    if not any(l.strip() == ".." for l in lines):
        dot_idx = next(i for i, l in enumerate(lines) if l.strip() == ".")
        lines.insert(dot_idx + 1, "..")
    pth.write_text("\n".join(l.replace("#import site", "import site") for l in lines),
                   encoding="utf-8")

    # 离线引导 pip: 本机 pip 下载 wheel, 内嵌 python 直接从 wheel 里跑 pip
    wheel_dir = zip_cache / "wheels"
    wheel_dir.mkdir(exist_ok=True)
    _log([sys.executable, "-m", "pip", "download", "pip", "-d", wheel_dir, "-q"])
    subprocess.run([sys.executable, "-m", "pip", "download", "pip",
                    "-d", str(wheel_dir), "-q"],
                   check=True, shell=False, cwd=str(REPO_ROOT))
    pip_wheel = next(wheel_dir.glob("pip-*.whl"))
    # pip 是纯 Python 包: 直接解压 wheel 到 site-packages, python -m pip 即可用
    # (比 wheel/pip 自举法可靠 — 后者要求 pip 已安装)
    site_packages = emb_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pip_wheel) as zf:
        zf.extractall(str(site_packages))
    _log([py_exe, "-m", "pip", "--version"])
    subprocess.run([str(py_exe), "-m", "pip", "--version"],
                   check=True, shell=False, cwd=str(REPO_ROOT))

    if not skip_deps:
        _assert_public_https_host(index_url)
        _log([py_exe, "-m", "pip", "install", "-r", "requirements.txt", "-i", index_url])
        subprocess.run([str(py_exe), "-m", "pip", "install",
                        "-r", str(REPO_ROOT / "requirements.txt"),
                        "-i", index_url, "--no-warn-script-location", "-q"],
                       check=True, shell=False, cwd=str(REPO_ROOT))

    # 依赖导入自检
    _log([py_exe, "-c", "import <deps>; print('deps ok')"])
    subprocess.run(
        [str(py_exe), "-c",
         "import fastapi, uvicorn, httpx, sqlalchemy, aiosqlite, apscheduler, "
         "pydantic_settings, dotenv, openai, bs4, playwright; print('deps ok')"],
        check=True, shell=False, cwd=str(REPO_ROOT))


def copy_source(pkg_root: Path, source: str) -> None:
    print("  复制源码 (git 追踪文件, 排除 .env/data/tests)")
    repo = str(REPO_ROOT)
    if source == "head":
        blob = subprocess.run(["git", "archive", "--format=tar", "HEAD"],
                              cwd=repo, check=True, shell=False,
                              capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
            tf.extractall(str(pkg_root), filter="data")
    else:  # worktree: 含未提交修改
        files = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=repo, check=True, shell=False, capture_output=True,
            text=True, encoding="utf-8").stdout.splitlines()
        for rel in files:
            if ".." in PurePosixPath(rel.replace("\\", "/")).parts:
                continue
            src = REPO_ROOT / rel
            if not src.is_file():
                continue
            dst = pkg_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    for junk in pkg_root.rglob("__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)
    # 测试与部署脚本对便携版无用, 减体积
    for name in ("tests", "scripts", ".githooks", ".gitee", ".github"):
        shutil.rmtree(pkg_root / name, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="打包 AIMC Windows 便携版")
    ap.add_argument("--source", choices=["worktree", "head"], default="worktree",
                    help="worktree=含未提交修改 (默认), head=仅已提交内容")
    ap.add_argument("--skip-deps", action="store_true",
                    help="不预装依赖 (包小, 但首次启动需联网 pip install)")
    ap.add_argument("--index-url", default=DEFAULT_INDEX_URL, help="pip 镜像 (仅 https)")
    args = ap.parse_args()

    version = _read_version()
    print(f"== 打包 AIMC Portable v{version} (source={args.source}) ==")

    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    pkg = BUILD_DIR
    pkg.mkdir(parents=True)

    setup_embedded_python(pkg / "python", args.skip_deps, args.index_url)
    copy_source(pkg, args.source)

    # 启动资产 (first_run_setup.py 只随源码进 portable/ 子目录 — bat 调用的就是
    # portable\first_run_setup.py, 其 ROOT=parent.parent 正确指向包根;
    # 不要再往包根拷贝, 否则该脚本的相对定位会把 .env 生成到包外面)
    shutil.copy2(REPO_ROOT / "portable" / "start-aimc.bat", pkg / "start-aimc.bat")
    shutil.copy2(REPO_ROOT / "portable" / "stop-aimc.bat", pkg / "stop-aimc.bat")
    # 使用说明带 UTF-8 BOM, 记事本不乱码
    (pkg / "使用说明.txt").write_text(USAGE_TXT, encoding="utf-8-sig")

    DIST_DIR.mkdir(exist_ok=True)
    out = DIST_DIR / f"AIMC-Portable-v{version}.zip"
    print(f"== 压缩 {out.name} ==")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(pkg.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(pkg.parent))

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"== 完成: {out} ({size_mb:.0f} MB) ==")
    print("验收: 解压到任意目录, 双击 start-aimc.bat, 浏览器应自动打开看板。")


if __name__ == "__main__":
    main()
