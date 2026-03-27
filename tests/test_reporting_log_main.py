from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

import main
from agents import TokenCounter
from core import log as logger
from core.reporter import _clean, generate
from memory.embeddings import RemoteEmbeddings


class FakeConsole:
    def __init__(self):
        self.lines = []
        self.width = 100

    def print(self, *args, **kwargs):
        self.lines.append((args, kwargs))


class FakeMemory:
    def __init__(self):
        self.created = False

    def get_profile(self, session_id):
        return {"service": "nginx", "host": "localhost", "baseline_rps": 100, "best_rps": 120}

    def get_facts(self, session_id):
        return [
            {
                "type": "fix",
                "parameter": "p",
                "before_value": "a",
                "after_value": "b",
                "before_rps": 100,
                "after_rps": 120,
                "impact_pct": 20,
                "reasoning": "why",
            }
        ]

    def get_queue(self, session_id):
        return [{"name": "h1", "priority": 1, "status": "done", "outcome": "ok"}]

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.disconnected = True

    def session_exists(self, session_id):
        return False

    def create_session(self, **kwargs):
        self.created = True


def test_reporter_generate_and_clean(tmp_path):
    memory = FakeMemory()
    tc = TokenCounter(input_tokens=10, output_tokens=5, tool_calls=2)
    tc.add_tool_tokens("inspect", calls=1, call_input=2, call_output=1, post_input=3, post_output=1)
    path = generate(
        "s1",
        memory,
        tc,
        output_dir=str(tmp_path),
        baselines={"small": {"rps": 100, "p99": 1, "cpu_pct": 1, "mem_mb": 1}},
        finals={"small": {"rps": 120, "p99": 1, "cpu_pct": 2, "mem_mb": 2}},
        throughput={"nic_speed": "1g", "disk_write": "100MB/s", "small_throughput_mb_s": 10},
        token_history=[
            {
                "session_id": "s1",
                "created_at": "2024-01-01T00:00:00",
                "input_tokens": 1,
                "output_tokens": 2,
                "total_tokens": 3,
                "tool_calls": 4,
            }
        ],
    )
    assert Path(path).exists()
    md = Path(path).read_text()
    assert "Token Attribution by Tool" in md
    report_json = json.loads((tmp_path / "report.json").read_text())
    assert report_json["tokens"]["by_tool"][0]["tool"] == "inspect"
    assert _clean({"a": 1, "embedding": [1]}) == {"a": 1}

    class EmptyMemory(FakeMemory):
        def get_facts(self, session_id):
            return []

    tc2 = TokenCounter()
    path2 = generate(
        "s2",
        EmptyMemory(),
        tc2,
        output_dir=str(tmp_path),
        stability={
            "duration_sec": 60,
            "sample_count": 1,
            "mean_rps": 1.0,
            "stdev_rps": 0.0,
            "cv_pct": 0.0,
            "samples": [1.0],
        },
    )
    assert "No fixes were applied." in Path(path2).read_text()


def test_logger_writes_and_cleans(tmp_path, monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(logger, "_console", fake_console)
    path = logger.init("sid", verbose=True, log_dir=str(tmp_path))
    logger.log("agent", "hello\nworld", "info")
    logger.llm_call("agent", "\x1b[31mcall\x1b[0m")
    logger.tool_call("inspect", "run")
    logger.tool_result("inspect", "done")
    logger.step("Step X")
    logger.status("main", "ready")
    logger.check("cpu", "a, b, c", "warning", "fix")
    logger.benchmark("Baseline", 1.0, 2.0, 3.0, 4.0)
    logger.panel("Title", "line1\nline2")
    logger.tokens("agent", 1, 2, "in=1")
    logger.close()
    assert Path(path).exists()
    assert "hello world" in Path(path).read_text()
    assert fake_console.lines


def test_main_helpers_and_main_flow(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\n#x\nB='two'\nINVALID\n")
    monkeypatch.setattr(main.logger, "status", lambda *a, **k: None)

    class FakePath(Path):
        _flavour = type(Path())._flavour

    monkeypatch.setattr(main, "Path", FakePath)
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    main.load_dotenv()
    assert "A" in main.os.environ

    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(yaml.safe_dump({"x": 1}))
    assert main.load_config(str(cfg_file))["x"] == 1
    chunks = main._chunk_markdown("## One\nA\n## Two\nB", "f.md")
    assert chunks[0]["title"] == "One"

    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "a.md").write_text("## T\nBody")
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    logs = []
    monkeypatch.setattr(main.logger, "status", lambda *a, **k: logs.append(a))

    class Embedder:
        def embed(self, text):
            return [1.0]

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            pass

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass

    monkeypatch.setattr("pymysql.connect", lambda **kwargs: Conn())
    main.load_knowledge(
        {"memory": {"host": "h", "port": 1, "user": "u", "database": "d"}}, Embedder(), object()
    )
    assert (facts_dir / ".loaded_hash").exists()

    cfg = {
        "llm": {
            "active_profile": "v",
            "profiles": {"v": {"backend": "ollama", "model": "m", "base_url": "u"}},
        },
        "target": {"host": "localhost"},
        "service": {"name": "nginx"},
    }
    monkeypatch.setitem(
        main.sys.modules,
        "langchain_ollama",
        SimpleNamespace(ChatOllama=lambda **kwargs: ("ollama", kwargs)),
    )
    monkeypatch.setitem(
        main.sys.modules,
        "langchain_anthropic",
        SimpleNamespace(ChatAnthropic=lambda **kwargs: ("anthropic", kwargs)),
    )
    monkeypatch.setattr(main.logger, "log", lambda *a, **k: None)
    assert main.get_model(cfg)[0] == "ollama"

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        main.get_model(
            {"llm": {"active_profile": "c", "profiles": {"c": {"backend": "claude", "model": "m"}}}}
        )
        assert False
    except SystemExit:
        pass
    try:
        main.get_model(
            {"llm": {"active_profile": "x", "profiles": {"x": {"backend": "bad", "model": "m"}}}}
        )
        assert False
    except SystemExit:
        pass

    cfg_main = {
        "llm": {
            "active_profile": "v",
            "profiles": {"v": {"backend": "ollama", "model": "m", "base_url": "u"}},
        },
        "target": {"host": "localhost"},
        "service": {"name": "nginx"},
    }
    monkeypatch.setattr(main, "load_config", lambda p: cfg_main)
    monkeypatch.setattr(main.logger, "init", lambda *a, **k: "log")
    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "embedder_from_config", lambda cfg: "embed")
    fake_memory = FakeMemory()
    monkeypatch.setattr(main, "tidb_from_config", lambda cfg, embed: fake_memory)
    monkeypatch.setattr(main, "load_knowledge", lambda *a, **k: None)
    fake_ssh = SimpleNamespace(connect=lambda: None, disconnect=lambda: None)
    monkeypatch.setattr(main, "ssh_from_config", lambda cfg, section="target": fake_ssh)
    monkeypatch.setattr(main, "load_adapter", lambda cfg, ssh, bench=None: "adapter")
    monkeypatch.setattr(main, "get_model", lambda cfg: "model")
    monkeypatch.setattr(main.logger, "status", lambda *a, **k: None)
    monkeypatch.setattr(main.logger, "log", lambda *a, **k: None)
    monkeypatch.setattr(main.logger, "close", lambda: None)
    monkeypatch.setitem(
        main.sys.modules,
        "core.orchestrator",
        SimpleNamespace(run=lambda model, deps: asyncio.sleep(0, result="report.md")),
    )
    asyncio.run(main.main("cfg.yaml", None, False))
    assert fake_memory.created is True


def test_load_knowledge_skip_when_hash_unchanged(tmp_path, monkeypatch):
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    doc = facts_dir / "a.md"
    doc.write_text("hello")
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    current_hash = main.hashlib.md5(doc.read_bytes()).hexdigest()
    (facts_dir / ".loaded_hash").write_text(current_hash)
    calls = []
    monkeypatch.setattr(main.logger, "status", lambda *a, **k: calls.append(a))
    main.load_knowledge(
        {"memory": {"host": "h", "port": 1, "user": "u", "database": "d"}}, object(), object()
    )
    assert any("unchanged, skipping load" in args[1] for args in calls if len(args) > 1)


def test_remote_embeddings(monkeypatch):
    class FakeEmbeddingsAPI:
        def create(self, model, input):
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 2.0])])

    class FakeAnthropic:
        def __init__(self, api_key=None):
            self.embeddings = FakeEmbeddingsAPI()

    monkeypatch.setitem(main.sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))
    emb = RemoteEmbeddings("voyage-3")
    assert emb.embed("hello") == [1.0, 2.0]
