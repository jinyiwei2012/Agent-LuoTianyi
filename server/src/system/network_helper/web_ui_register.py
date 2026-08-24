import os
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def register_web_ui(app: "FastAPI", current_dir: str) -> None:
    """将 Web 网页版前端（Vite 构建产物）注册到 FastAPI 应用中。

    页面托管在根路径 ``/``，使同一端口同时提供聊天页面与管理控制台。
    构建产物位于仓库根目录的 ``web/dist``，缺失时返回提示 HTML。
    """
    web_build = os.path.join(os.path.dirname(current_dir), "web", "dist")
    web_assets = os.path.join(web_build, "assets")
    if os.path.isdir(web_assets):
        app.mount("/assets", StaticFiles(directory=web_assets), name="web-assets")

    index_path = os.path.join(web_build, "index.html")

    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    async def web_index(path: str = ""):
        # 避免接管已有的 API / 管理后台 / 项目计划书路径
        if path and path.split("/")[0] in {
            "admin",
            "project-plan",
            "assets",
            "auth",
            "preference",
            "history",
            "dynamics",
            "get_image",
            "update_image_client_path",
            "llm",
            "docs",
        }:
            return HTMLResponse(status_code=404, content="Not Found")
        if os.path.exists(index_path):
            return FileResponse(index_path, headers={"Cache-Control": "no-store"})
        return HTMLResponse(
            "<h1>洛天依 Agent Web</h1>"
            "<p>Web 前端尚未构建。请运行 <code>cd web && npm install && npm run build</code> 后重启服务。</p>"
        )
