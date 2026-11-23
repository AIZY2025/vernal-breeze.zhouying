"""core/settings.py 分支补充测试：配置加载与团队装配。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import _support  # noqa: F401  （把项目根目录加入 sys.path）

from core.agents import LLMTeam
from core.llm import LLMError, OpenAICompatible
from core.mock import MockTeam
from core.settings import build_quant, build_research, build_team, load_config
from core import quant


class BuildQuantTest(unittest.TestCase):
    def setUp(self):
        self.original = quant.configure()
        self.addCleanup(lambda: quant.configure(self.original))

    def test_missing_section_keeps_defaults(self):
        conf = build_quant({})
        self.assertEqual(conf["emergency_months"], 6)

    def test_custom_section_is_applied(self):
        build_quant({"quant": {"emergency_months": 9}})
        self.assertEqual(quant.PARAMS["emergency_months"], 9)

    def test_invalid_value_exits(self):
        with self.assertRaises(SystemExit):
            build_quant({"quant": {"sensitivity_pct": "bad"}})


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(load_config(self.dir / "nope.json"), {})

    def test_valid_json_is_parsed(self):
        path = self.dir / "config.json"
        path.write_text(json.dumps({"provider": "mock", "model": "m1"}, ensure_ascii=False),
                        encoding="utf-8")
        cfg = load_config(path)
        self.assertEqual(cfg["provider"], "mock")
        self.assertEqual(cfg["model"], "m1")

    def test_invalid_json_raises_system_exit(self):
        path = self.dir / "config.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            load_config(path)
        self.assertIn("config.json 不是合法 JSON：", str(ctx.exception))

    def test_accepts_string_path(self):
        path = self.dir / "config.json"
        path.write_text('{"model": "x"}', encoding="utf-8")
        self.assertEqual(load_config(str(path)), {"model": "x"})

    def test_non_object_top_level_rejected(self):
        path = self.dir / "config.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            load_config(path)
        self.assertIn("顶层必须是 JSON 对象", str(ctx.exception))


class BuildTeamMockTest(unittest.TestCase):
    def test_force_mock(self):
        team, note = build_team({"provider": "real", "api_key": "k"}, force="mock")
        self.assertIsInstance(team, MockTeam)
        self.assertEqual(note, "预置剧本（不调用模型）")

    def test_auto_without_key_falls_back_to_mock(self):
        with patch.dict(os.environ, {}, clear=True):
            team, note = build_team({"provider": "auto", "base_url": "http://x", "model": "m"})
        self.assertIsInstance(team, MockTeam)
        self.assertEqual(note, "预置剧本（不调用模型）")

    def test_default_provider_is_auto(self):
        with patch.dict(os.environ, {}, clear=True):
            team, _ = build_team({})
        self.assertIsInstance(team, MockTeam)

    def test_mock_provider_ignores_present_key(self):
        team, note = build_team({"provider": "mock", "api_key": "k"})
        self.assertIsInstance(team, MockTeam)
        self.assertEqual(note, "预置剧本（不调用模型）")


class BuildTeamLLMTest(unittest.TestCase):
    def test_missing_key_raises_system_exit_with_env_name(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                build_team({"provider": "real", "api_key_env": "MY_API_KEY",
                            "base_url": "http://x", "model": "m"})
        message = str(ctx.exception)
        self.assertIn("选择了真实模型但 api_key 为空", message)
        self.assertIn("MY_API_KEY", message)

    def test_default_env_name_in_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                build_team({"provider": "real", "base_url": "http://x", "model": "m"})
        self.assertIn("SANDBOX_API_KEY", str(ctx.exception))

    def test_key_from_environment(self):
        fake_team = MagicMock(name="LLMTeam")
        cfg = {"provider": "real", "base_url": "https://api.test", "model": "m1",
               "api_key_env": "MY_API_KEY", "timeout": "30", "max_tokens": "512",
               "temperature": {"A1": 0.2}}
        with patch.dict(os.environ, {"MY_API_KEY": "env-key"}, clear=True):
            with patch("core.settings.LLMTeam", fake_team) as team_cls:
                team, note = build_team(cfg)
        client = team_cls.call_args.args[0]
        self.assertIsInstance(client, OpenAICompatible)
        self.assertEqual(client.api_key, "env-key")
        self.assertEqual(client.base_url, "https://api.test")
        self.assertEqual(client.model, "m1")
        self.assertEqual(client.timeout, 30)
        self.assertEqual(client.max_tokens, 512)
        self.assertEqual(team_cls.call_args.args[1], {"A1": 0.2})
        self.assertIs(team, fake_team.return_value)
        self.assertEqual(note, "真实模型 m1")

    def test_key_from_config_uses_default_timeout_and_temperature(self):
        fake_team = MagicMock(name="LLMTeam")
        cfg = {"provider": "real", "api_key": "cfg-key",
               "base_url": "https://api.test/", "model": "m2"}
        with patch.dict(os.environ, {}, clear=True):
            with patch("core.settings.LLMTeam", fake_team) as team_cls:
                _, note = build_team(cfg)
        client = team_cls.call_args.args[0]
        self.assertEqual(client.api_key, "cfg-key")
        self.assertEqual(client.base_url, "https://api.test")
        self.assertEqual(client.timeout, 90)
        self.assertEqual(client.max_tokens, 3000)
        self.assertEqual(team_cls.call_args.args[1], {})
        self.assertEqual(note, "真实模型 m2")

    def test_empty_temperature_falls_back_to_empty_dict(self):
        fake_team = MagicMock(name="LLMTeam")
        cfg = {"provider": "real", "api_key": "k", "base_url": "https://api.test",
               "model": "m", "temperature": {}}
        with patch("core.settings.LLMTeam", fake_team) as team_cls:
            build_team(cfg)
        self.assertEqual(team_cls.call_args.args[1], {})

    def test_missing_base_url_raises_llm_error(self):
        with self.assertRaises(LLMError) as ctx:
            build_team({"provider": "real", "api_key": "k", "model": "m"})
        self.assertEqual(str(ctx.exception), "base_url 为空，无法调用模型")

    def test_missing_model_raises_llm_error(self):
        with self.assertRaises(LLMError) as ctx:
            build_team({"provider": "real", "api_key": "k", "base_url": "http://x"})
        self.assertEqual(str(ctx.exception), "model 为空，请填写模型 ID")

    def test_real_team_loads_prompts(self):
        cfg = {"provider": "real", "api_key": "k", "base_url": "https://api.test", "model": "m3"}
        team, note = build_team(cfg)
        self.assertIsInstance(team, LLMTeam)
        self.assertEqual(sorted(team.prompts), ["A1", "A2", "A3", "A4", "A5"])
        self.assertTrue(team.prompts["A1"].strip())
        self.assertEqual(note, "真实模型 m3")

    def test_non_integer_timeout_rejected(self):
        cfg = {"provider": "real", "api_key": "k", "base_url": "http://x", "model": "m",
               "timeout": "abc"}
        with self.assertRaises(SystemExit) as ctx:
            build_team(cfg)
        self.assertIn("timeout 必须是整数", str(ctx.exception))

    def test_non_integer_max_tokens_rejected(self):
        cfg = {"provider": "real", "api_key": "k", "base_url": "http://x", "model": "m",
               "max_tokens": "很多"}
        with self.assertRaises(SystemExit) as ctx:
            build_team(cfg)
        self.assertIn("max_tokens 必须是整数", str(ctx.exception))


class BuildResearchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_no_search_section_disables_layer(self):
        self.assertIsNone(build_research({}))

    def test_enabled_false_disables_layer(self):
        self.assertIsNone(build_research({"search": {"enabled": False}}))

    def test_mode_off_disables_layer(self):
        self.assertIsNone(build_research({"search": {"mode": "off"}}))

    def test_invalid_mode_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            build_research({"search": {"mode": "fast"}})
        self.assertIn("search.mode", str(ctx.exception))

    def test_config_values_are_mapped(self):
        service = build_research({"search": {
            "api_key": "sk-1", "mode": "live", "timeout": "5",
            "max_items": "3", "cache_hours": "12"}}, self.dir)
        self.assertEqual(service.client.api_key, "sk-1")
        self.assertEqual(service.client.timeout, 5.0)
        self.assertEqual(service.max_items, 3)
        self.assertEqual(service.cache_hours, 12.0)
        self.assertEqual(service.cache_dir, self.dir)
        self.assertTrue(service.active)

    def test_key_from_environment(self):
        with patch.dict(os.environ, {"WSA_API_KEY": "sk-env"}, clear=True):
            service = build_research({"search": {"api_key_env": "WSA_API_KEY"}})
        self.assertEqual(service.client.api_key, "sk-env")

    def test_secret_credentials_from_config(self):
        service = build_research({"search": {
            "secret_id": "AKID1", "secret_key": "sk", "mode": "live"}})
        self.assertTrue(service.client.signed)
        self.assertEqual(service.client.endpoint, "https://wsa.tencentcloudapi.com")
        self.assertTrue(service.active)

    def test_secret_credentials_from_environment(self):
        env = {"TENCENTCLOUD_SECRET_ID": "AKIDenv",
               "TENCENTCLOUD_SECRET_KEY": "sk-env"}
        with patch.dict(os.environ, env, clear=True):
            service = build_research({"search": {}})
        self.assertTrue(service.client.signed)
        self.assertEqual(service.client.secret_id, "AKIDenv")

    def test_non_numeric_timeout_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            build_research({"search": {"timeout": "慢"}})
        self.assertIn("search.timeout", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()