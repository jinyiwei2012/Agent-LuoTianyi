"""本地复刻 .github/workflows/ci.yml 的离线门禁。

推送分支或创建 / 更新 PR 之前先跑本脚本，把 CI 的检查在本地做一遍，
不要用“等 CI 结果”代替第一道验证。

用法：
    python scripts/local_ci.py --jobs all
    python scripts/local_ci.py --jobs auto
    python scripts/local_ci.py --jobs server-lint,desktop-tests
    python scripts/local_ci.py --list

退出码 0 表示所选作业全部通过；任一作业失败即返回非 0。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

# 与 .github/workflows/ci.yml 的 env 保持一致，只用于配置解析，不授予真实服务权限。
CI_ENV = {
    "CI": "true",
    "PYTHONUTF8": "1",
    "QWEN_API_KEY": "offline-test-key",
    "SILICONFLOW_API_KEY": "offline-test-key",
    "AMAP_KEY": "offline-test-key",
    "JWT_SECRET": "offline-ci-secret-not-for-production",
    "RUN_EXTERNAL_TESTS": "0",
    "RUN_REAL_LLM_TESTS": "0",
}

SERVER_DIR = ROOT / "server"
CLIENT_DIR = ROOT / "client"
CLI_DIR = ROOT / "cli-client"
APP_DIR = ROOT / "app"
ADMIN_DIR = SERVER_DIR / "res" / "admin_ui"

# 本地检查产物统一放这里，避免污染工作区；CI 在临时环境中直接写仓库路径。
RESULTS_DIR = ROOT / ".ci-results"


def env() -> dict[str, str]:
    merged = os.environ.copy()
    merged.update(CI_ENV)
    lychee = find_lychee()
    if lychee:
        # 保证 .github/scripts/docs_ci.py 调用的 "lychee" 能被解析到已安装版本。
        merged["PATH"] = str(lychee.parent) + os.pathsep + merged.get("PATH", "")
    return merged


def find_lychee() -> Path | None:
    """按 PATH、本地工具目录依次查找 lychee（CI 固定 0.24.2）。"""
    found = shutil.which("lychee")
    if found:
        return Path(found)
    candidates = [
        Path.home() / ".local" / "bin" / "lychee.exe",
        Path.home() / ".local" / "bin" / "lychee",
        ROOT / ".tools" / "lychee" / "lychee.exe",
        ROOT / ".tools" / "lychee" / "lychee",
    ]
    return next((path for path in candidates if path.exists()), None)


def lychee_version(path: Path) -> str:
    probe = subprocess.run([str(path), "--version"], capture_output=True, text=True)
    return (probe.stdout or probe.stderr).strip()


def run(cmd: list[str], cwd: Path, extra_env: dict[str, str] | None = None) -> None:
    resolved = list(cmd)
    if shutil.which(resolved[0]):
        resolved[0] = shutil.which(resolved[0])  # Windows 下 npm / npx 实际是 .cmd
    print(f"\n$ [{cwd.name}] {' '.join(cmd)}", flush=True)
    local_env = env()
    if extra_env:
        local_env.update(extra_env)
    code = subprocess.run(resolved, cwd=str(cwd), env=local_env).returncode
    if code:
        raise SystemExit(f"作业失败（退出码 {code}）：{' '.join(cmd)}")


def ensure_server_config() -> None:
    """CI 在测试前从公开模板生成配置；本地同样处理，但绝不覆盖已有配置。"""
    config = SERVER_DIR / "config" / "config.json"
    template = SERVER_DIR / "config" / "config.json.template"
    if not config.exists():
        shutil.copyfile(template, config)
        print(f"[准备] 由模板生成 {config.relative_to(ROOT)}")


def find_base(explicit: str | None) -> str:
    candidates = [explicit] if explicit else ["upstream/dev", "origin/dev", "dev"]
    for ref in candidates:
        if not ref:
            continue
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        if exists.returncode == 0:
            return subprocess.run(
                ["git", "merge-base", ref, "HEAD"],
                cwd=str(ROOT), capture_output=True, text=True, check=True,
            ).stdout.strip()
    raise SystemExit("找不到可用的 base 分支，请用 --base 指定（例如 --base upstream/dev）")


def job_changes(base: str) -> bool:
    classified = subprocess.run(
        [PY, ".github/scripts/docs_ci.py", "classify", base],
        cwd=str(ROOT), env=env(), capture_output=True, text=True,
    )
    print(classified.stdout.strip() or classified.stderr.strip())
    if classified.returncode:
        raise SystemExit("改动范围检查失败")
    docs_only = "docs_only=true" in classified.stdout
    if docs_only:
        lychee = find_lychee()
        if lychee is None:
            print(
                "[未验证] 未找到 lychee：纯文档改动的 Markdown 链接检查无法本地复刻。"
                "请在 PR 的验证一节标注这一项未在本地验证，不能写成通过。",
                file=sys.stderr,
            )
        else:
            version = lychee_version(lychee)
            print(f"lychee: {version} ({lychee})")
            if "0.24.2" not in version:
                print("[版本差异] lychee 版本与 CI 固定的 0.24.2 不一致，结果可能不同。", file=sys.stderr)
            run([PY, ".github/scripts/docs_ci.py", "check", base], ROOT)
    return docs_only


def job_server_lint() -> None:
    run([PY, "-m", "ruff", "check", ".", "--output-format=concise"], SERVER_DIR)
    run([PY, "-m", "black", "--check", "."], SERVER_DIR)
    run([PY, "-m", "ruff", "check", "../.github/scripts/check_pytest_results.py"], SERVER_DIR)
    run(
        [PY, "-m", "black", "--check", "--include", r".*\.py$",
         "../.github/scripts/check_pytest_results.py"],
        SERVER_DIR,
    )


def job_server_tests() -> None:
    ensure_server_config()
    report = RESULTS_DIR / "server-test-results.xml"
    run(
        [PY, "-m", "pytest", "tests",
         "-m", "not external and not real_llm",
         "--ignore=tests/e2e/external",
         "-q", "--tb=short", f"--junitxml={report}"],
        SERVER_DIR,
    )
    run([PY, "../.github/scripts/check_pytest_results.py", str(report)], SERVER_DIR)


def job_desktop_tests() -> None:
    report = RESULTS_DIR / "desktop-test-results.xml"
    run(
        [PY, "-m", "pytest", "tests",
         "--ignore=tests/test_login.py",
         "--ignore=tests/test_communication.py",
         "--ignore=tests/test_ws_concurrent.py",
         "--ignore=tests/test_audio_base64.py",
         "-q", "--tb=short", f"--junitxml={report}"],
        CLIENT_DIR,
        extra_env={"QT_QPA_PLATFORM": "offscreen"},
    )
    run([PY, "../.github/scripts/check_pytest_results.py", str(report)], CLIENT_DIR)


def job_cli_tests() -> None:
    report = RESULTS_DIR / "cli-test-results.xml"
    run(
        [PY, "-m", "pytest", "tests", "-q", "--tb=short", f"--junitxml={report}"],
        CLI_DIR,
    )
    run([PY, "../.github/scripts/check_pytest_results.py", str(report)], CLI_DIR)


def job_app_checks() -> None:
    ensure_server_config()
    report = RESULTS_DIR / "app-test-results.json"
    run(["npm", "run", "lint"], APP_DIR)
    run(["npx", "--no-install", "tsc", "--noEmit"], APP_DIR)
    run(
        ["npx", "--no-install", "jest", "--runInBand", "--json", f"--outputFile={report}"],
        APP_DIR,
        # 与 CI 一致：指定启动本机回环服务端的 Python，否则相关用例会被静默 skip。
        extra_env={"CALL_E2E_PYTHON": PY},
    )
    results = json.loads(report.read_text(encoding="utf-8"))
    if (
        not results.get("success")
        or results.get("numTotalTests", 0) == 0
        or results.get("numPendingTests", 0) > 0
        or results.get("numPendingTestSuites", 0) > 0
    ):
        raise SystemExit("移动端离线测试未全部执行并通过")
    run(["node", "--test", "scripts/test-live2d-blink.cjs", "scripts/test-live2d-touch.cjs"], APP_DIR)


def job_admin_build(skip_install: bool) -> None:
    if not skip_install:
        run(["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"], ADMIN_DIR)
    # CI 直接写入仓库内的 admin_static；本地改用临时输出目录，避免改写被跟踪的构建产物。
    run(
        ["npm", "run", "build", "--", "--outDir", str(RESULTS_DIR / "admin-build"), "--emptyOutDir"],
        ADMIN_DIR,
    )


JOBS = {
    "changes": None,
    "server-lint": job_server_lint,
    "server-tests": job_server_tests,
    "desktop-tests": job_desktop_tests,
    "cli-tests": job_cli_tests,
    "app-checks": job_app_checks,
    "admin-build": job_admin_build,
}


def auto_jobs(base: str) -> list[str]:
    changed = subprocess.run(
        ["git", "diff", "--name-only", base, "HEAD"],
        cwd=str(ROOT), capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    selected = {"changes"}
    for path in changed:
        if path.startswith("server/res/admin_ui/"):
            selected.add("admin-build")
        elif path.startswith("server/") or path.startswith(".github/"):
            selected.update({"server-lint", "server-tests"})
        elif path.startswith("client/"):
            selected.add("desktop-tests")
        elif path.startswith("cli-client/"):
            selected.add("cli-tests")
        elif path.startswith("app/"):
            selected.add("app-checks")
        elif path.startswith("docs/"):
            continue
        else:
            # 无法归类（根目录脚本、配置等）时按全量处理，避免漏检。
            selected.update({"server-lint", "server-tests", "desktop-tests",
                             "cli-tests", "app-checks", "admin-build"})
    order = list(JOBS)
    return [name for name in order if name in selected]


def check_toolchain() -> None:
    problems: list[str] = []
    if sys.version_info[:2] != (3, 10):
        problems.append(f"Python 为 {sys.version_info.major}.{sys.version_info.minor}，CI 使用 3.10")
    for module, expected in (("ruff", "0.14.10"), ("black", "26.5.1")):
        probe = subprocess.run([PY, "-m", module, "--version"], capture_output=True, text=True)
        text = (probe.stdout or probe.stderr).strip().splitlines()
        version = text[0] if text else "未知"
        if expected not in version:
            problems.append(f"{module} 版本为 {version}，CI 固定 {expected}")
    for warning in problems:
        print(f"[版本差异] {warning}", file=sys.stderr)
    if problems:
        print("[提示] 本地通过不能等同于 CI 通过；请按 CI 版本复跑或在 PR 中注明差异。", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="本地复刻 ci.yml 的离线检查")
    parser.add_argument("--jobs", default="auto",
                        help="all、auto 或逗号分隔的作业名；默认 auto")
    parser.add_argument("--base", default=None, help="对比基线，默认依次尝试 upstream/dev、origin/dev、dev")
    parser.add_argument("--skip-install", action="store_true", help="admin-build 跳过 npm ci")
    parser.add_argument("--list", action="store_true", help="列出可用作业后退出")
    args = parser.parse_args()

    if args.list:
        print("\n".join(JOBS))
        return

    check_toolchain()
    base = find_base(args.base)
    RESULTS_DIR.mkdir(exist_ok=True)

    if args.jobs == "auto":
        selected = auto_jobs(base)
    elif args.jobs == "all":
        selected = list(JOBS)
    else:
        selected = [name.strip() for name in args.jobs.split(",") if name.strip()]
    unknown = [name for name in selected if name not in JOBS]
    if unknown:
        raise SystemExit(f"未知作业：{', '.join(unknown)}；可用作业见 --list")

    print(f"base: {base}")
    print(f"jobs: {', '.join(selected)}\n")

    docs_only = False
    for name in selected:
        print(f"===== {name} =====", flush=True)
        if name == "changes":
            docs_only = job_changes(base)
            continue
        if docs_only:
            print("[跳过] 纯文档改动，CI 不运行该作业")
            continue
        if name == "admin-build":
            job_admin_build(args.skip_install)
        else:
            JOBS[name]()

    print("\n全部所选作业通过")


if __name__ == "__main__":
    main()
