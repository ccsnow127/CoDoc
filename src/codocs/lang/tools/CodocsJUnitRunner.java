package com.codocs;

import java.io.ByteArrayOutputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.io.PrintStream;
import java.io.UnsupportedEncodingException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import org.junit.platform.engine.DiscoverySelector;
import org.junit.platform.engine.TestExecutionResult;
import org.junit.platform.engine.TestSource;
import org.junit.platform.engine.discovery.DiscoverySelectors;
import org.junit.platform.engine.support.descriptor.ClassSource;
import org.junit.platform.engine.support.descriptor.MethodSource;
import org.junit.platform.launcher.Launcher;
import org.junit.platform.launcher.LauncherDiscoveryRequest;
import org.junit.platform.launcher.TestExecutionListener;
import org.junit.platform.launcher.TestIdentifier;
import org.junit.platform.launcher.TestPlan;
import org.junit.platform.launcher.core.LauncherDiscoveryRequestBuilder;
import org.junit.platform.launcher.core.LauncherFactory;

/**
 * JUnit Platform launcher for codocs: streams one JSON object per line to an event file.
 *
 * <p>events: run, result, container_fail, container_skip, discovered, discovery_error,
 * timeout, end. exit codes: 0 all passed, 1 some failed, 2 discovery error, 3 failfast halt,
 * 124 own timeout.
 */
public final class CodocsJUnitRunner {
    private static final int TEXT_CAP = 16000;
    private static final int CAPTURE_CAP = 256 * 1024;
    private static final Object LOCK = new Object();
    private static PrintStream events;
    private static volatile boolean anyFailed;
    private static boolean failfast;
    private static TestPlan plan;
    private static final ByteArrayOutputStream CAPTURED = new ByteArrayOutputStream();
    private static volatile boolean capturing;
    private static final Set<String> REPORTED = new HashSet<>();

    private CodocsJUnitRunner() {
    }

    static String q(String s) {
        if (s == null) {
            s = "";
        }
        StringBuilder b = new StringBuilder(s.length() + 2);
        b.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20 || Character.isSurrogate(c)) {
                        b.append(String.format("\\u%04x", (int) c));
                    } else {
                        b.append(c);
                    }
            }
        }
        b.append('"');
        return b.toString();
    }

    static void emit(String json) {
        synchronized (LOCK) {
            events.println(json);
            events.flush();
        }
    }

    static String cap(String s) {
        return s.length() <= TEXT_CAP ? s : s.substring(s.length() - TEXT_CAP);
    }

    static final class Tee extends OutputStream {
        private final PrintStream orig;

        Tee(PrintStream orig) {
            this.orig = orig;
        }

        @Override
        public void write(int b) {
            orig.write(b);
            if (capturing) {
                synchronized (CAPTURED) {
                    if (CAPTURED.size() < CAPTURE_CAP) {
                        CAPTURED.write(b);
                    }
                }
            }
        }

        @Override
        public void write(byte[] b, int off, int len) {
            orig.write(b, off, len);
            if (capturing) {
                synchronized (CAPTURED) {
                    int room = CAPTURE_CAP - CAPTURED.size();
                    if (room > 0) {
                        CAPTURED.write(b, off, Math.min(len, room));
                    }
                }
            }
        }

        @Override
        public void flush() {
            orig.flush();
        }
    }

    static void startCapture() {
        synchronized (CAPTURED) {
            CAPTURED.reset();
        }
        capturing = true;
    }

    static String stopCapture() {
        capturing = false;
        System.out.flush();
        System.err.flush();
        synchronized (CAPTURED) {
            String s = new String(CAPTURED.toByteArray(), StandardCharsets.UTF_8);
            CAPTURED.reset();
            return s;
        }
    }

    static boolean noise(String frame) {
        return frame.startsWith("org.junit.") || frame.startsWith("jdk.internal.")
            || frame.startsWith("java.base/jdk.internal.") || frame.startsWith("java.lang.reflect.")
            || frame.startsWith("java.base/java.lang.reflect.") || frame.startsWith("sun.")
            || frame.startsWith("com.codocs.") || frame.startsWith("java.base/java.util.")
            || frame.startsWith("java.util.") || frame.startsWith("org.opentest4j.");
    }

    static String describe(Throwable t) {
        if (t == null) {
            return "";
        }
        StringBuilder b = new StringBuilder();
        Throwable cur = t;
        int depth = 0;
        Set<Throwable> seen = new HashSet<>();
        while (cur != null && depth < 4 && seen.add(cur)) {
            if (depth > 0) {
                b.append("Caused by: ");
            }
            b.append(cur.toString()).append('\n');
            int kept = 0;
            for (StackTraceElement e : cur.getStackTrace()) {
                String f = e.toString();
                if (noise(f)) {
                    continue;
                }
                b.append("\tat ").append(f).append('\n');
                if (++kept >= (depth == 0 ? 8 : 3)) {
                    break;
                }
            }
            cur = cur.getCause();
            depth++;
        }
        return b.toString();
    }

    static MethodSource methodSource(TestIdentifier t) {
        Optional<TestSource> s = t.getSource();
        if (s.isPresent() && s.get() instanceof MethodSource) {
            return (MethodSource) s.get();
        }
        return null;
    }

    static ClassSource classSource(TestIdentifier t) {
        Optional<TestSource> s = t.getSource();
        if (s.isPresent() && s.get() instanceof ClassSource) {
            return (ClassSource) s.get();
        }
        return null;
    }

    /** class#method of the test (or its nearest ancestor with a method source); null if none. */
    static String fold(TestIdentifier t) {
        TestIdentifier cur = t;
        while (cur != null) {
            MethodSource m = methodSource(cur);
            if (m != null) {
                return m.getClassName() + "#" + m.getMethodName();
            }
            Optional<TestIdentifier> parent = plan == null ? Optional.empty() : plan.getParent(cur);
            cur = parent.orElse(null);
        }
        return null;
    }

    static void result(String id, String uid, String outcome, String text) {
        synchronized (REPORTED) {
            REPORTED.add(uid);
        }
        emit("{\"e\":\"result\",\"id\":" + q(id) + ",\"uid\":" + q(uid) + ",\"outcome\":"
            + q(outcome) + ",\"text\":" + q(cap(text)) + "}");
    }

    /** report every not-yet-reported method below a failed or skipped container. */
    static void descendants(TestIdentifier container, String outcome, String text) {
        for (TestIdentifier d : plan.getDescendants(container)) {
            if (methodSource(d) == null) {
                continue;
            }
            boolean done;
            synchronized (REPORTED) {
                done = REPORTED.contains(d.getUniqueId());
            }
            if (!done) {
                result(fold(d), d.getUniqueId(), outcome, text);
            }
        }
    }

    static void collectMethods(Class<?> k, String name, List<Method> out, Set<String> sigs,
                               Set<Class<?>> seen) {
        if (k == null || k == Object.class || !seen.add(k)) {
            return;
        }
        for (Method m : k.getDeclaredMethods()) {
            if (m.getName().equals(name) && !m.isSynthetic() && !m.isBridge()) {
                String sig = name + java.util.Arrays.toString(m.getParameterTypes());
                if (sigs.add(sig)) {
                    out.add(m);
                }
            }
        }
        collectMethods(k.getSuperclass(), name, out, sigs, seen);
        for (Class<?> i : k.getInterfaces()) {
            collectMethods(i, name, out, sigs, seen);
        }
    }

    static Class<?> load(String name) throws ClassNotFoundException {
        return Class.forName(name, false, Thread.currentThread().getContextClassLoader());
    }

    static void fail(String text) {
        emit("{\"e\":\"discovery_error\",\"text\":" + q(cap(text)) + "}");
        System.out.flush();
        System.err.flush();
        Runtime.getRuntime().halt(2);
    }

    public static void main(String[] args) throws IOException {
        String eventsPath = null;
        long timeout = 0;
        boolean discoverOnly = false;
        List<String> classes = new ArrayList<>();
        List<String> methods = new ArrayList<>();
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            if (a.equals("--events")) {
                eventsPath = args[++i];
            } else if (a.equals("--timeout")) {
                timeout = Long.parseLong(args[++i]);
            } else if (a.equals("--failfast")) {
                failfast = true;
            } else if (a.equals("--discover-only")) {
                discoverOnly = true;
            } else if (a.equals("--select-class")) {
                classes.add(args[++i]);
            } else if (a.equals("--select-method")) {
                methods.add(args[++i]);
            } else if (a.equals("--select-file")) {
                for (String line : Files.readAllLines(Paths.get(args[++i]), StandardCharsets.UTF_8)) {
                    line = line.trim();
                    if (line.startsWith("c:")) {
                        classes.add(line.substring(2));
                    } else if (line.startsWith("m:")) {
                        methods.add(line.substring(2));
                    }
                }
            }
        }
        if (eventsPath == null) {
            System.err.println("usage: CodocsJUnitRunner --events FILE [--timeout S] [--failfast] "
                + "[--discover-only] (--select-class C | --select-method C#m | --select-file F)...");
            System.exit(2);
        }
        try {
            events = new PrintStream(new FileOutputStream(eventsPath, true), true, "UTF-8");
        } catch (UnsupportedEncodingException e) {
            throw new IOException(e);
        }
        if (timeout > 0) {
            final long ms = timeout * 1000L;
            Thread watchdog = new Thread(() -> {
                try {
                    Thread.sleep(ms);
                } catch (InterruptedException e) {
                    return;
                }
                emit("{\"e\":\"timeout\"}");
                System.out.flush();
                System.err.flush();
                Runtime.getRuntime().halt(124);
            }, "codocs-watchdog");
            watchdog.setDaemon(true);
            watchdog.start();
        }
        try {
            System.setOut(new PrintStream(new Tee(System.out), true, "UTF-8"));
            System.setErr(new PrintStream(new Tee(System.err), true, "UTF-8"));
        } catch (UnsupportedEncodingException e) {
            throw new IOException(e);
        }

        List<DiscoverySelector> sel = new ArrayList<>();
        Set<String> classSet = new LinkedHashSet<>(classes);
        for (String c : classSet) {
            try {
                sel.add(DiscoverySelectors.selectClass(load(c)));
            } catch (Throwable t) {
                fail("cannot load test class " + c + ": " + t);
            }
        }
        for (String spec : new LinkedHashSet<>(methods)) {
            int h = spec.lastIndexOf('#');
            if (h <= 0) {
                continue;
            }
            String cname = spec.substring(0, h);
            String mname = spec.substring(h + 1);
            Class<?> k;
            try {
                k = load(cname);
            } catch (Throwable t) {
                fail("cannot load test class " + cname + ": " + t);
                return;
            }
            List<Method> found = new ArrayList<>();
            try {
                collectMethods(k, mname, found, new HashSet<>(), new HashSet<>());
            } catch (Throwable t) {
                fail("cannot inspect test class " + cname + ": " + t);
            }
            for (Method m : found) {
                sel.add(DiscoverySelectors.selectMethod(k, m));
            }
        }
        if (sel.isEmpty()) {
            emit("{\"e\":\"end\",\"failed\":false}");
            System.exit(0);
        }
        Launcher launcher = LauncherFactory.create();
        LauncherDiscoveryRequest req = LauncherDiscoveryRequestBuilder.request().selectors(sel).build();
        try {
            plan = launcher.discover(req);
        } catch (Throwable t) {
            fail("test discovery failed: " + describe(t));
            return;
        }
        if (discoverOnly) {
            Set<String> ids = new LinkedHashSet<>();
            for (TestIdentifier root : plan.getRoots()) {
                for (TestIdentifier d : plan.getDescendants(root)) {
                    if (methodSource(d) != null) {
                        ids.add(fold(d));
                    }
                }
            }
            for (String id : ids) {
                emit("{\"e\":\"discovered\",\"id\":" + q(id) + "}");
            }
            emit("{\"e\":\"end\",\"failed\":false}");
            System.exit(0);
        }
        launcher.execute(plan, new TestExecutionListener() {
            @Override
            public void executionStarted(TestIdentifier t) {
                if (t.isTest()) {
                    String id = fold(t);
                    if (id != null) {
                        startCapture();
                        emit("{\"e\":\"run\",\"id\":" + q(id) + ",\"uid\":" + q(t.getUniqueId())
                            + "}");
                    }
                }
            }

            @Override
            public void executionSkipped(TestIdentifier t, String reason) {
                String text = reason == null ? "" : reason;
                if (t.isTest() || methodSource(t) != null) {
                    String id = fold(t);
                    if (id != null) {
                        result(id, t.getUniqueId(), "skipped", text);
                    }
                    if (t.isContainer()) {
                        descendants(t, "skipped", text);
                    }
                    return;
                }
                ClassSource cs = classSource(t);
                if (cs != null) {
                    emit("{\"e\":\"container_skip\",\"class\":" + q(cs.getClassName())
                        + ",\"text\":" + q(cap(text)) + "}");
                    descendants(t, "skipped", text);
                }
            }

            @Override
            public void executionFinished(TestIdentifier t, TestExecutionResult r) {
                String thrown = describe(r.getThrowable().orElse(null));
                if (t.isTest()) {
                    String id = fold(t);
                    if (id == null) {
                        return;
                    }
                    String out = stopCapture();
                    String outcome;
                    switch (r.getStatus()) {
                        case SUCCESSFUL: outcome = "passed"; break;
                        case ABORTED: outcome = "aborted"; break;
                        default: outcome = "failed";
                    }
                    String text = out.isEmpty() ? thrown : (thrown.isEmpty() ? out : out + "\n" + thrown);
                    result(id, t.getUniqueId(), outcome, text);
                    if (outcome.equals("failed")) {
                        anyFailed = true;
                        if (failfast) {
                            System.out.flush();
                            System.err.flush();
                            Runtime.getRuntime().halt(3);
                        }
                    }
                    return;
                }
                if (r.getStatus() == TestExecutionResult.Status.SUCCESSFUL) {
                    return;
                }
                String outcome = r.getStatus() == TestExecutionResult.Status.ABORTED ? "skipped"
                    : "failed";
                if (methodSource(t) != null) {
                    String id = fold(t);
                    boolean done;
                    synchronized (REPORTED) {
                        done = REPORTED.contains(t.getUniqueId());
                    }
                    if (id != null && !done) {
                        result(id, t.getUniqueId(), outcome, thrown);
                    }
                } else {
                    ClassSource cs = classSource(t);
                    String cls = cs == null ? "" : cs.getClassName();
                    emit("{\"e\":\"" + (outcome.equals("failed") ? "container_fail" : "container_skip")
                        + "\",\"class\":" + q(cls) + ",\"text\":" + q(cap(thrown)) + "}");
                }
                descendants(t, outcome, thrown);
                if (outcome.equals("failed")) {
                    anyFailed = true;
                    if (failfast) {
                        System.out.flush();
                        System.err.flush();
                        Runtime.getRuntime().halt(3);
                    }
                }
            }
        });
        emit("{\"e\":\"end\",\"failed\":" + anyFailed + "}");
        System.out.flush();
        System.err.flush();
        System.exit(anyFailed ? 1 : 0);
    }
}
