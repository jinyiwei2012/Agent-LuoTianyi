"""Image understanding capability integration tests."""

import base64
import os
import sys
from pathlib import Path

import pytest

server_root = str(Path(__file__).resolve().parent.parent)
if server_root not in sys.path:
    sys.path.insert(0, server_root)

from src.capabilities.image_understanding import ImageUnderstanding
from src.utils.helpers import load_config
from src.utils.llm_service import LLMService


@pytest.fixture(scope="module", autouse=True)
def server_cwd():
    old_cwd = os.getcwd()
    os.chdir(server_root)
    try:
        yield
    finally:
        os.chdir(old_cwd)


@pytest.fixture(scope="module")
def sample_image(tmp_path_factory):
    """生成一张临时测试图片，避免依赖 gitignored 的运行时 data/ 目录。"""
    from PIL import Image, ImageDraw

    image_path = tmp_path_factory.mktemp("vlm_test_images") / "sample.png"
    image = Image.new("RGB", (64, 64), color=(200, 120, 60))
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 8, 56, 56), outline=(255, 255, 255), width=4)
    image.save(image_path, format="PNG")
    return image_path


@pytest.fixture(scope="module")
def full_config():
    return load_config("config/config.json")


@pytest.fixture(scope="module")
def image_understanding(full_config):
    llm_service = LLMService(full_config["llm_service"])
    capability = ImageUnderstanding(full_config["capabilities"]["image_understanding"])
    capability.create_vlm_module(llm_service)
    return capability


def test_vlm_config_is_valid(full_config):
    capabilities_cfg = full_config.get("capabilities", {})
    image_cfg = capabilities_cfg.get("image_understanding", {})
    module_cfg = image_cfg.get("vlm_module", {})
    vlm_cfg = module_cfg.get("vlm", {})

    assert vlm_cfg.get("name") in full_config["llm_service"]["available_vlms"]
    assert module_cfg.get("prompt_name") == "vision_interaction_prompt"
    assert Path("res/agent/prompts/vision_interaction_prompt.json").exists()


@pytest.mark.real_llm
@pytest.mark.asyncio
async def test_vlm_describes_image_with_non_empty_content(image_understanding, sample_image):
    """真实 VLM 调用（需要已配置的 VLM API Key），默认跳过。"""
    image_b64 = base64.b64encode(sample_image.read_bytes()).decode("utf-8")
    image_data_uri = f"data:image/png;base64,{image_b64}"

    description = await image_understanding.describe_image(image_data_uri)

    assert isinstance(description, str)
    assert description.startswith("[一张图片]:")
    content = description.removeprefix("[一张图片]:").strip()
    assert content, "VLM description should contain content after the image prefix"
