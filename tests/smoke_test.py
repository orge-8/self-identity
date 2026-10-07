#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""self-identity 包式加载冒烟（FakeHost）。

按 runtime-gotchas §22.2 复刻真机加载方式：spec_from_file_location 传
submodule_search_locations（包式），并主动把插件目录从 sys.path 摘掉；
另用干净子进程跑 §22.1 五秒探针，兜住「残留平铺延迟导入」。

用法: python tests/smoke_test.py
退出码: 0=全绿, 1=有失败
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import logging
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PKG_NAME = "self_identity_plugin_under_test"

PASS = 0
FAIL = 0
FAILURES: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} {detail}")


def load_plugin_package() -> types.ModuleType:
    """复刻 Runner 的包式加载：目录作为包、目录不在 sys.path 上。"""
    plugin_dir_str = str(PLUGIN_DIR)
    while plugin_dir_str in sys.path:
        sys.path.remove(plugin_dir_str)
    spec = importlib.util.spec_from_file_location(
        PKG_NAME, PLUGIN_DIR / "plugin.py",
        submodule_search_locations=[plugin_dir_str],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- FakeHost


class FakePaths:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def data_dir(self) -> Path:
        p = self.root / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def runtime_dir(self) -> Path:
        p = self.root / "runtime"
        p.mkdir(parents=True, exist_ok=True)
        return p


class FakeHost:
    """模拟 Host：send.* 捕获、config.get 返回 bot.qq_account、llm/message 可配置。"""

    def __init__(self, qq_account: str = "123456789", llm_response: str = "", recent_messages: Optional[list] = None) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="self-identity-fake-"))
        self.paths = FakePaths(self.root)
        self.qq_account = qq_account
        self.llm_response = llm_response
        self.recent_messages = recent_messages or []
        self.calls: List[tuple] = []

    async def rpc_call(self, method: str, plugin_id: str = "", payload: Optional[dict] = None, **kw: Any) -> Any:
        kw2 = dict(payload or {})
        capability = kw2.get("capability") or method
        args = kw2.get("args") or {}
        self.calls.append((capability, args))
        if capability == "config.get":
            if str(args.get("key") or "") == "bot.qq_account":
                return {"success": True, "value": self.qq_account}
            return {"success": True, "value": None}
        if capability == "llm.generate":
            return {"success": True, "response": self.llm_response, "model": "fake-vlm"}
        if capability == "message.get_recent":
            return {"success": True, "messages": self.recent_messages}
        if capability.startswith("send."):
            return True
        return {"success": True, "result": None}

    def calls_of(self, capability: str) -> List[dict]:
        return [args for cap, args in self.calls if cap == capability]

    @property
    def sent_texts(self) -> List[str]:
        return [str(args.get("text") or "") for args in self.calls_of("send.text")]


def build_context(plugin_id: str, host: FakeHost):
    from maibot_sdk.context import PluginContext

    return PluginContext(plugin_id, host.rpc_call, host.paths)


def default_config(module: types.ModuleType, tmp_root: Path) -> Dict[str, Any]:
    return {
        "plugin": {"enabled": True, "config_version": module.SUPPORTED_CONFIG_VERSION},
        "identity_image": {
            "image_dir": str(tmp_root / "images"),
            "thumbnail_dir": str(tmp_root / "thumbs"),
        },
        "profile": {"name": "小镜", "appearance": "浅蓝长发，金黄色瞳孔", "personality": "活泼温柔又敬业"},
        "infos": [
            {"title": "名字", "keywords": ["名字", "称呼"], "full_information": "大家叫我小镜"},
            {"title": "喜欢的食物", "keywords": ["食物", "喜好"], "full_information": "喜欢草莓蛋糕"},
            {"title": "外貌", "keywords": ["外貌", "发型", "发色"], "full_information": "浅蓝长发"},
        ],
    }


_MIN_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def seed_png_bytes() -> bytes:
    """最小合法 PNG 字节（VisionOutput/比对用例用）。"""
    return _MIN_PNG


def seed_images(image_dir: Path, count: int = 2) -> List[Path]:
    """生成真实可开的小图（PIL 优先；缺失时写入最小 PNG）。"""
    image_dir.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    try:
        from PIL import Image

        for i in range(count):
            path = image_dir / f"persona_{i}.png"
            Image.new("RGB", (16, 16), (i * 60 % 255, 96, 160)).save(path, format="PNG")
            paths.append(path)
    except ImportError:
        for i in range(count):
            path = image_dir / f"persona_{i}.png"
            path.write_bytes(_MIN_PNG)
            paths.append(path)
    return paths


async def run_smoke(module: types.ModuleType) -> None:
    tmp_root = Path(tempfile.mkdtemp(prefix="self-identity-data-"))
    image_dir = tmp_root / "images"
    seed_images(image_dir, 2)

    host = FakeHost()
    plugin = module.create_plugin()
    plugin._set_context(build_context(module.PLUGIN_ID, host))
    plugin.set_plugin_config(default_config(module, tmp_root))

    # ---- 生命周期：on_load
    await plugin.on_load()
    check("on_load 后图库已扫描", len(plugin._gallery) == 2, f"实际 {len(plugin._gallery)}")
    check("on_load 后档案已加载", len(plugin._infos) == 3, f"实际 {len(plugin._infos)}")

    # ---- Tool：search_self_information
    result = await plugin.search_self_information(keyword="食物")
    check("search 命中食物", result.get("success") and "草莓蛋糕" in result.get("content", ""), str(result)[:120])
    result = await plugin.search_self_information()
    check("search 空条件被拒", not result.get("success") and "至少提供" in result.get("content", ""), str(result)[:120])
    result = await plugin.search_self_information(keyword="食物 运动")
    check("search AND 未全命中排除", not result.get("success"), str(result)[:120])

    # ---- Tool：view_all_image
    result = await plugin.view_all_image(page=1)
    check("view_all_image 列出 2 张", result.get("success") and result.get("total_count") == 2, str(result)[:160])
    result99 = await plugin.view_all_image(page=99)
    check("view_all_image 越界夹取", result99.get("page") == 1 and "夹取" in result99.get("content", ""), str(result99)[:160])

    # ---- Tool：get_self_image
    result = await plugin.get_self_image(image_index=1)
    check("get_self_image 按序号", result.get("success") and result.get("image_index") == 1, str(result)[:120])
    result = await plugin.get_self_image(image_index=9)
    check("get_self_image 越界报错", not result.get("success") and "超出范围" in result.get("content", ""), str(result)[:120])
    result = await plugin.get_self_image(image_name="persona_0")
    check("get_self_image 按名", result.get("success") and result.get("image_name") == "persona_0.png", str(result)[:120])
    result = await plugin.get_self_image(image_name="random")
    check("get_self_image 随机", result.get("success") and result.get("image_name"), str(result)[:120])

    # ---- Tool：get_self_avatar（下载打桩 → 二次命中缓存）
    async def _fake_download(url: str, **kwargs: Any):
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        ), "image/png"

    original_download = module.fetch_si.download_image_bytes
    module.fetch_si.download_image_bytes = _fake_download
    try:
        result = await plugin.get_self_avatar()
        check("get_self_avatar 首次下载", result.get("success") and result.get("content_items"), str(result)[:160])
        check("get_self_avatar URL 正确", result.get("avatar_url", "").endswith("nk=123456789&s=640"))
        result = await plugin.get_self_avatar()
        check("get_self_avatar 二次缓存命中", result.get("success") and "缓存命中" in result.get("content", ""), str(result)[:160])
    finally:
        module.fetch_si.download_image_bytes = original_download

    # 无效 QQ 号分支
    host_bad = FakeHost(qq_account="not-a-qq")
    plugin_bad = module.create_plugin()
    plugin_bad._set_context(build_context(module.PLUGIN_ID, host_bad))
    plugin_bad.set_plugin_config(default_config(module, tmp_root))
    result = await plugin_bad.get_self_avatar()
    check("get_self_avatar 非法 QQ 拒绝", not result.get("success") and "有效" in result.get("content", ""), str(result)[:120])

    # ---- Tool：verify_self_claim（v1.1.0）
    result = await plugin.verify_self_claim(claim="我是浅蓝长发的")
    check("verify_self_claim 符合", result.get("success") and result.get("state") == "符合", str(result)[:160])
    result = await plugin.verify_self_claim(claim="我擅长开飞机")
    check("verify_self_claim 无记录", result.get("state") == "档案无记录", str(result)[:160])
    result = await plugin.verify_self_claim(claim="我叫雪见")
    check("verify_self_claim 名字冲突", result.get("state") == "冲突", str(result)[:160])

    # ---- Tool：compare_with_self_image（v1.1.0，VLM 打桩）
    host.llm_response = (
        '{"same_person": true, "verdict": "符合", "confidence": 0.77, '
        '"similarities": ["浅蓝长发", "发型一致"], "differences": ["服装不同"]}'
    )
    png_b64 = base64.b64encode(seed_png_bytes()).decode()
    result = await plugin.compare_with_self_image(image_base64=png_b64, image_index=1)
    check("compare_with_self_image 成功", result.get("success") and result.get("verdict") == "符合", str(result)[:180])
    check("compare 返回依据", "发型一致" in str(result.get("similarities")), str(result.get("similarities")))
    check("compare 传了 task_name", any(
        args.get("task_name") == "vlm" for args in host.calls_of("llm.generate")
    ), str(host.calls_of("llm.generate"))[:160])
    check("compare 运行期抬升 max_tokens", all(
        int(args.get("max_tokens") or 0) >= module.compare_si.VISION_MAX_TOKENS_FLOOR
        for args in host.calls_of("llm.generate")
    ))
    host.llm_response = "这张图挺好看的"
    result = await plugin.compare_with_self_image(image_base64=png_b64, image_index=1)
    check("compare 跑偏输出报错可读", not result.get("success") and "JSON" in result.get("content", ""), str(result)[:160])

    # ---- Command：四条命令（带 stream_id → 拦截级别 2 + send.text 已捕获）
    ok, text, level = await plugin.cmd_persona_status(stream_id="fake-stream")
    check("/人设状态 拦截级别=2", ok and level == 2, f"ok={ok} level={level}")
    check("/人设状态 文本完整", "档案条目" in text and "人设图库" in text, text[:160])
    check("/人设状态 报被动模式", "运行模式：被动" in text, text[:200])
    ok, text, level = await plugin.cmd_persona_refresh(stream_id="fake-stream")
    check("/人设刷新 成功", ok and "已刷新" in text and level == 2, text[:160])
    ok, text, level = await plugin.cmd_persona_gallery(stream_id="fake-stream")
    check("/人设图库 清单", ok and "persona_0.png" in text and level == 2, text[:200])
    ok, text, level = await plugin.cmd_persona_card(stream_id="fake-stream")
    check("/人设卡片 输出可粘贴卡片", ok and "我是小镜" in text and "behavior_style" in text and level == 2, text[:200])
    check("send.text 已捕获 4 次", len(host.sent_texts) == 4, f"实际 {len(host.sent_texts)}")
    # 无 stream_id：放行不拦截
    ok, _text, level = await plugin.cmd_persona_status()
    check("无 stream_id 放行(level=0)", ok and level == 0, f"level={level}")

    # ---- 配置热重载：换目录后 on_config_update 真重载
    new_image_dir = tmp_root / "images2"
    seed_images(new_image_dir, 1)
    new_config = default_config(module, tmp_root)
    new_config["identity_image"]["image_dir"] = str(new_image_dir)
    plugin.set_plugin_config(new_config)
    await plugin.on_config_update("test", {}, "")
    check("配置热重载后图库换目录", len(plugin._gallery) == 1 and plugin._gallery[0].name == "persona_0.png",
          f"实际 {[r.name for r in plugin._gallery]}")
    result = await plugin.search_self_information(keyword="食物")
    check("配置热重载后档案仍可用", result.get("success"), str(result)[:120])

    # ---- 组件注册
    components = plugin.get_components()
    names = {c.get("name") for c in components}
    expected = {"search_self_information", "view_all_image", "get_self_image", "get_self_avatar",
                "verify_self_claim", "compare_with_self_image",
                "persona_status", "persona_refresh", "persona_gallery", "persona_card"}
    check("组件数=10", len(components) == 10, f"实际 {len(components)}：{sorted(names)}")
    check("组件名集合一致", names == expected, f"实际 {sorted(names)}")
    check("无 hook 组件（v1.3.0 被动模式）",
          not [c for c in components if c.get("type") == "HOOK_HANDLER"],
          str([c.get("type") for c in components]))
    handler_names = {
        "search_self_information": "search_self_information",
        "view_all_image": "view_all_image",
        "get_self_image": "get_self_image",
        "get_self_avatar": "get_self_avatar",
        "verify_self_claim": "verify_self_claim",
        "compare_with_self_image": "compare_with_self_image",
        "persona_status": "cmd_persona_status",
        "persona_refresh": "cmd_persona_refresh",
        "persona_gallery": "cmd_persona_gallery",
        "persona_card": "cmd_persona_card",
    }
    for comp_name, method_name in handler_names.items():
        info = getattr(type(plugin), method_name).__maibot_component_info__
        check(f"装饰器绑定 {comp_name} → {method_name}", info.name == comp_name, f"实际 {info.name}")

    # ---- WebUI 配置页契约（v1.3.1：infos 必须在真实存在的 [general] 节里）
    schema = plugin.get_webui_config_schema(plugin_id=module.PLUGIN_ID)
    general = (schema.get("sections") or {}).get("general") or {}
    infos_field = (general.get("fields") or {}).get("infos") or {}
    check("配置页存在 [general] 节", bool(general) and general.get("title") == "自我信息",
          str(general.get("title")))
    check("配置页 infos 为对象列表控件",
          infos_field.get("ui_type") == "list" and infos_field.get("item_type") == "object",
          f"ui_type={infos_field.get('ui_type')} item_type={infos_field.get('item_type')}")
    check("配置页 infos 三个子字段齐全",
          set(infos_field.get("item_fields") or {}) == {"title", "keywords", "full_information"},
          str(list(infos_field.get("item_fields") or {})))
    check("配置页 infos 有 label 与 hint（description 不渲染）",
          bool(infos_field.get("label")) and bool(infos_field.get("hint")),
          f"label={infos_field.get('label')!r} hint={str(infos_field.get('hint'))[:40]!r}")

    # ---- on_unload
    await plugin.on_unload()
    check("on_unload 后内存已清", plugin._gallery == [] and plugin._infos == [])


def main() -> int:
    print("== self-identity 包式加载冒烟 ==")
    module = load_plugin_package()
    print("[OK] 包式加载成功（真机兼容）")
    logging.basicConfig(level=logging.WARNING)

    asyncio.run(run_smoke(module))

    # ---- 五秒探针（§22.1）：干净子进程验证真机加载条件
    print("== 真机加载探针 ==")
    probe = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util, sys\n"
         "from pathlib import Path\n"
         f"d = Path(r'{PLUGIN_DIR}').resolve()\n"
         "sys.path = [p for p in sys.path if str(d) != p]\n"
         "spec = importlib.util.spec_from_file_location('probe_pkg', d/'plugin.py', submodule_search_locations=[str(d)])\n"
         "m = importlib.util.module_from_spec(spec); sys.modules['probe_pkg'] = m\n"
         "spec.loader.exec_module(m)\n"
         "p = m.create_plugin()\n"
         "print('LOAD-OK')\n"],
        capture_output=True, text=True, timeout=60,
    )
    check("真机探针-包式加载 LOAD-OK", "LOAD-OK" in probe.stdout, (probe.stderr or probe.stdout)[-200:])

    print("=" * 40)
    print(f"冒烟结果: PASS {PASS} / FAIL {FAIL}")
    if FAILURES:
        print("失败项：")
        for item in FAILURES:
            print(f"  - {item}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
