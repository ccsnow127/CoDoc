"""end-to-end: `codocs run` on a copy of the javamod fixture with a scripted fake llm."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from _e2e_lang import (
    check_doc,
    check_resume,
    check_suite,
    check_summary,
    install,
    load,
    run_cli,
    snapshot,
    wiki_plan,
)

MVN = shutil.which("mvn")
JAVAC = shutil.which("javac")
pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(not (MVN and JAVAC), reason="javac/mvn not installed")]

CALC = "src/main/java/com/example/calc/Calc.java"
STATS = "src/main/java/com/example/calc/Stats.java"
RECT = "src/main/java/com/example/shapes/Rect.java"
SHAPES = "src/main/java/com/example/shapes/Shapes.java"

SUITES = {
    CALC: '''package com.example.calc;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

class AnyName {
    @Test
    void clamp() {
        assertEquals(5, Calc.clamp(5, 0, 10));
        assertEquals(0, Calc.clamp(-1, 0, 10));
        assertEquals(10, Calc.clamp(11, 0, 10));
    }

    @Test
    void sum() {
        assertEquals(6, Calc.sum(new int[] {1, 2, 3}));
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> Calc.sum(new int[0]));
        assertEquals("calc: empty input", e.getMessage());
        assertThrows(IllegalArgumentException.class, () -> Calc.sum(null));
    }

    @Test
    void normalize() {
        assertEquals("hello-world", Calc.normalize("  Hello World "));
        assertEquals(10, Calc.SCALE);
    }

    @Test
    void wrong() {
        assertEquals(1, Calc.clamp(1, 2, 3));
    }
}
''',
    STATS: '''package com.example.calc;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class StatsCodocsTest {
    @Test
    void addAndMean() {
        Stats s = new Stats();
        assertEquals(0, s.mean());
        s.add(3);
        s.add(7);
        s.add(5);
        assertEquals(3, s.getCount());
        assertEquals(7, s.getMax());
        assertEquals(50, s.mean());
    }

    @Test
    void firstValueIsMax() {
        Stats s = new Stats();
        s.add(-4);
        assertEquals(-4, s.getMax());
    }
}
''',
    RECT: '''package com.example.shapes;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class RectCodocsTest {
    @Test
    void area() {
        assertEquals(6, new Rect(2, 3).area());
    }

    @Test
    void name() {
        assertEquals("square", new Rect(2, 2).name());
        assertEquals("rect", new Rect(2, 3).name());
    }
}
''',
    SHAPES: '''package com.example.shapes;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;

import java.util.List;
import org.junit.jupiter.api.Test;

class ShapesCodocsTest {
    @Test
    void classify() {
        assertEquals("small", Shapes.classify(new Rect(2, 3)));
        assertEquals("medium", Shapes.classify(new Rect(5, 5)));
        assertEquals("large", Shapes.classify(new Rect(10, 10)));
    }

    @Test
    void largest() {
        Rect a = new Rect(1, 1);
        Rect b = new Rect(3, 3);
        assertSame(b, Shapes.largest(List.of(a, b, new Rect(2, 2))));
        assertNull(Shapes.largest(List.of()));
    }
}
''',
}
TARGETS = sorted(SUITES)


def _suite_name(rel: str) -> str:
    return f"{Path(rel).stem}CodocsTest.java"


def test_java_run_end_to_end_and_resume(tmp_path: Path, monkeypatch):
    repo, out, fake = install(monkeypatch, tmp_path, "javamod", SUITES)
    before = snapshot(repo)
    res = run_cli(repo, out)
    assert res.exit_code == 0, res.output

    analysis = load(out / "analysis.json")
    assert analysis["languages"] == ["java"]
    assert sorted(t["file"] for t in analysis["targets"] if t["status"] == "ok") == TARGETS

    base = load(out / "baseline" / "java" / "src__main__java__com__example__calc.json")
    if not base["build_ok"] and any("resolve" in e or "offline" in e
                                    for e in base["build_errors"]):
        pytest.skip(f"java tool jars unavailable: {base['build_errors'][:1]}")
    assert base["build_ok"] is True and base["lang"] == "java"
    assert base["failing"] == ["com.example.calc.CalcTest#knownBroken"]
    assert set(base["passing"]) >= {"com.example.calc.CalcTest#clamp",
                                    "com.example.calc.StatsTest#mean"}

    for rel in TARGETS:
        check_suite(out, rel, _suite_name(rel), min_cov=0.8)
    calc = load(out / "testgen" / str(Path(CALC).parent) / "Calc.java" / "result.json")
    assert calc["best_size"] == 3
    delivered = out / "tests" / str(Path(CALC).parent) / "CalcCodocsTest.java"
    text = delivered.read_text()
    assert "class CalcCodocsTest" in text and "void wrong" not in text

    # the delivered suites pass with maven in a clean copy of the fixture (not offline: the
    # user's ~/.m2 may not hold the surefire plugin yet)
    check = tmp_path / "check"
    shutil.copytree(repo, check)
    for rel in TARGETS:
        dest = check / str(Path(rel).parent).replace("src/main/", "src/test/")
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(out / "tests" / str(Path(rel).parent) / _suite_name(rel), dest)
    run = subprocess.run([MVN, "-q", "-B", "test", "-Dtest=*CodocsTest",
                          "-Dsurefire.failIfNoSpecifiedTests=false", "-Djacoco.skip=true"],
                         cwd=check, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    reports = list((check / "target" / "surefire-reports").glob("TEST-*CodocsTest.xml"))
    assert len(reports) == 4, reports

    for rel in TARGETS:
        check_doc(out, rel)
    sig = {rel: load(out / "docgen" / str(Path(rel).parent) / Path(rel).name /
                     "result.json")["signal"] for rel in TARGETS}
    assert sig == {CALC: "tests", STATS: "tests", RECT: "generated-tests",
                   SHAPES: "generated-tests"}
    doc = (out / "docs" / str(Path(CALC).parent) / "markdown_docs" / "Calc.java.md").read_text()
    assert "**Package**: `com.example.calc` (module `javamod`)" in doc
    assert "## MethodDef Calc.clamp" in doc

    check_summary(out, "java", docs=4, suites=4)
    calls = len(fake.calls)
    check_resume(repo, out, fake)
    assert snapshot(repo) == before

    text, plan = wiki_plan(repo, out)
    assert set(plan["languages"]) == {"java"} and plan["leaf_out"] == 2, plan
    assert len(fake.calls) == calls
