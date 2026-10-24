"""Go prompt context and failure feedback: panic summaries, imported apis, test references
and reply budgets."""

from codocs.golang.package import PackageInfo, imported_api
from codocs.golang.package import test_reference as build_test_reference
from codocs.specforge.loop import MAX_REPLY_TOKENS, codegen_max_tokens, doc_max_tokens
from codocs.tesearch.measure import failure_message, failure_summary
from codocs.tesearch.prompts import mark_failing, seed_messages

PANIC_OUT = """2026/10/14 16:59:15 [app] [log] (2026-10-14 16:59:15.201)
--- FAIL: TestNotificationProcess (0.00s)
panic: runtime error: invalid memory address or nil pointer dereference [recovered]
	panic: runtime error: invalid memory address or nil pointer dereference
[signal SIGSEGV: segmentation violation code=0x1 addr=0x10 pc=0x957ad7]

goroutine 40 [running]:
testing.tRunner.func1.2({0x9d4dc0, 0xf4e140})
	/home/u/.local/go/src/testing/testing.go:1632 +0x230
panic({0x9d4dc0?, 0xf4e140?})
	/home/u/.local/go/src/runtime/panic.go:791 +0x132
sync.(*RWMutex).RLock(...)
	/home/u/.local/go/src/sync/rwmutex.go:70
example.com/m/metrics.(*Collector).Count(0x0, {0xa1b2c3, 0x4})
	/home/u/m/metrics/metrics.go:120 +0x37
example.com/m/registry.ProcessNotification({0x0, 0x0})
	/home/u/m/registry/process.go:45 +0x1a5
"""


def test_failure_summary_names_panic_and_module_frames():
    s = failure_summary(PANIC_OUT)
    assert s.startswith("panic: runtime error: invalid memory address")
    assert "[recovered]" not in s
    assert "example.com/m/metrics.(*Collector).Count (metrics.go:120)" in s
    assert "ProcessNotification (process.go:45)" in s
    assert "testing." not in s and "sync." not in s and "2026/10/14" not in s


def test_failure_summary_assertions_and_log_noise():
    out = ("2026/10/14 16:59:15 [app] noise\n=== RUN   TestX\n"
           "    x_test.go:12: Get() = 3, want 4\n--- FAIL: TestX (0.00s)\n")
    assert failure_summary(out) == "x_test.go:12: Get() = 3, want 4"
    assert failure_summary("2026/10/14 16:59:15 only logs\n") == "test failed"


def test_failing_mark_uses_summary_not_log_line():
    msg = failure_message(PANIC_OUT)
    suite = 'package p\n\nimport "testing"\n\nfunc TestNotificationProcess(t *testing.T) {\n}\n'
    marked = mark_failing(suite, {"TestNotificationProcess": msg})
    assert "// <-- FAILING: panic: runtime error" in marked
    assert "[app]" not in marked.splitlines()[4]


def _module(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.21\n")
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "api.go").write_text(
        "package store\n\nvar (\n\tGet  = getImpl\n\tLock = SpinLock\n\tSpinLock = SpinLockFunc\n)\n\n"
        "func getImpl(key string) (string, error) { return \"\", nil }\n\n"
        "func SpinLockFunc(key string, ttl int) (bool, error) { return true, nil }\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "app.go").write_text(
        'package app\n\nimport "example.com/m/store"\n\n'
        "func Load(k string) string {\n\tv, _ := store.Get(k)\n\tok, _ := store.Lock(k, 1)\n"
        "\tif ok {\n\t\treturn v\n\t}\n\treturn \"\"\n}\n")
    return PackageInfo(rel="app", import_path="example.com/m/app", name="app",
                       go_files=["app/app.go"], test_files=["app/app_test.go"], xtest_files=[])


def test_imported_api_resolves_function_variable_types(tmp_path):
    pkg = _module(tmp_path)
    api = imported_api(tmp_path, pkg, "app/app.go")
    assert "var Get = getImpl  // type: func(key string) (string, error)" in api
    # chained alias: Lock = SpinLock = SpinLockFunc
    assert "var Lock = SpinLock  // type: func(key string, ttl int) (bool, error)" in api


def test_test_reference_prefers_relevant_helpers_and_example(tmp_path):
    pkg = _module(tmp_path)
    (tmp_path / "app" / "app_test.go").write_text(
        'package app\n\nimport (\n\t"testing"\n\n\t"example.com/m/store"\n)\n\n'
        "var unrelatedTable = []int{1, 2, 3}\n\n"
        "func setstubGet(t *testing.T, val string) {\n\torig := store.Get\n"
        "\tstore.Get = func(key string) (string, error) { return val, nil }\n"
        "\tt.Cleanup(func() { store.Get = orig })\n}\n\n"
        "func TestMain(m *testing.M) {\n\tinitLogger()\n\tm.Run()\n}\n\n"
        "func initLogger() {}\n\n"
        'func TestLoad(t *testing.T) {\n\tsetstubGet(t, "v")\n\tif Load("k") != "v" {\n'
        '\t\tt.Fatal("bad")\n\t}\n}\n')
    ref = build_test_reference(tmp_path, pkg, "app/app.go")
    assert "// example test of the target" in ref and "func TestLoad" in ref
    assert "func setstubGet" in ref and "func TestMain" in ref
    assert ref.index("func TestMain") < ref.index("unrelatedTable") if "unrelatedTable" in ref else True
    system, user = seed_messages("app/app.go", pkg, "package app\n", "", set(), [], ref)
    assert "[EXISTING TEST CODE OF THE PACKAGE" in user and "setstubGet" in user
    assert "cdx" in system


def test_reply_budgets_scale_with_file_size_and_are_capped():
    small, big, huge = "x" * 1000, "x" * 60000, "x" * 400000
    assert doc_max_tokens(small, 16000) == 16000
    assert doc_max_tokens(big, 16000) > 16000 > 0
    assert doc_max_tokens(huge, 16000) == MAX_REPLY_TOKENS
    assert codegen_max_tokens(huge, 16000) == MAX_REPLY_TOKENS
