import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_server import main


# Relevant OCS configuration and worker shapes, including the older fixed timer.
SCRIPT = """// ==UserScript==
// @connect      localhost
// @connect      172.20.0.2
// ==/UserScript==
  const AnswerWrapperHandlerConfig = {
    timeout_seconds: 60
  };
  function request(url, opts) {
    return new Promise((resolve, reject) => {
      const fet = fetch;
      fet(url, opts).then((response) => {
        response.json().then(resolve).catch(reject);
      }).catch(reject);
    });
  }
  async function defaultAnswerWrapperHandler(answererWrappers, env) {
    return Promise.all(answererWrappers.map(async (wrapper) => {
      const { method = "get", contentType = "json", type = "fetch", headers = {},
        data: requestData = {}, handler = "return (res) => [res.question, res.answer]" } = wrapper;
      const url = new URL(wrapper.url);
      try {
        const responseData = await Promise.race([
          request(url.toString(), {
            method,
            responseType: contentType,
            data: requestData,
            type,
            headers: JSON.parse(JSON.stringify(headers || {}))
          }),
          $.sleep(AnswerWrapperHandlerConfig.timeout_seconds * 1e3)
        ]);
        if (responseData === void 0) {
          throw new Error("题库请求超时，可能是题库问题，或者请检查网络或者重试。");
        }
        return { results: Function(handler)()(responseData) };
      } catch (error) {
        return { results: [], error: error.message || "题库连接失败" };
      }
    }));
  }
  function waitForRequested(result) {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        reject(new Error("答题超时！"));
      }, 60 * 1e3);
    });
  }
  const settings = {
          answerWrapperHandlerTimeout: {
            label: "搜题最大耗时（秒）",
            attrs: {
              type: "number",
              min: 10,
              step: 1,
              max: 3 * 60,
            },
            defaultValue: 120
          },
  };
  const unrelatedMediaTimeout = 3 * 60 * 1e3;
"""

JS_RUNTIME = os.environ.get("OCS_TEST_JS_RUNTIME") or shutil.which("node") or shutil.which("bun")


FAKE_CLOCK = """
const assert = require('node:assert/strict');
console.warn = () => {};
let now = 0, nextTimer = 0;
const timers = new Map();
globalThis.setTimeout = (callback, delay) => {
  const id = ++nextTimer;
  timers.set(id, { callback, time: now + delay });
  return id;
};
globalThis.clearTimeout = (id) => timers.delete(id);
async function settle(promise) {
  let done = false, value, failure;
  promise.then(result => { value = result; done = true; }, error => { failure = error; done = true; });
  for (let step = 0; !done && step < 100; step++) {
    for (let tick = 0; tick < 30; tick++) await Promise.resolve();
    if (done) break;
    assert(timers.size, 'promise stalled without a timer');
    const [id, timer] = [...timers].sort((a, b) => a[1].time - b[1].time)[0];
    timers.delete(id);
    now = timer.time;
    timer.callback();
  }
  assert(done, 'retry loop did not finish');
  assert.equal(timers.size, 0, 'request timers must be cleared');
  if (failure) throw failure;
  return value;
}
const wrapper = {url: 'https://bank.example/search', method: 'post', data: {title: '题目'}};
const answer = {question: '题目', answer: 'A'};
const ok = (data = answer) => ({ok: true, status: 200, json: async () => data});
"""


class UserscriptPatchTests(unittest.TestCase):
    def test_missing_script_skips_patch_before_config_or_network_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            script_path = Path(directory) / "missing.user.js"
            with patch.object(main.socket, "gethostbyname") as resolve, \
                    patch.object(main, "log_info"):
                main.patch_connect(script_path, {"OCS_ANSWER_TIMEOUT_SECONDS": "invalid"})
            resolve.assert_not_called()
            self.assertFalse(script_path.exists())

    def patch_script(self, script_path, env=None):
        with patch.object(main.socket, "gethostbyname", return_value="172.20.0.3"), \
                patch("builtins.print"):
            main.patch_connect(script_path, {} if env is None else env)

    def test_startup_patch_is_idempotent_and_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            script_path = Path(directory) / "ocs.user.js"
            script_path.write_text(SCRIPT, encoding="utf-8")
            self.patch_script(script_path)
            first = script_path.read_text(encoding="utf-8")
            self.patch_script(script_path)
            self.assertEqual(script_path.read_text(encoding="utf-8"), first)
        self.assertIn("// @connect      localhost", first)
        self.assertNotIn("// @connect      172.20.0.2", first)
        self.assertEqual(first.count("// @connect      172.20.0.3"), 1)
        self.assertEqual(first.count("// @connect      ocs-llm-answer"), 1)
        self.assertEqual(first.count("// OCS answer retry patch begin"), 1)
        self.assertEqual(first.count("// OCS answer HTTP status patch begin"), 1)
        self.assertIn("const unrelatedMediaTimeout = 3 * 60 * 1e3;", first)

    def test_invalid_budget_or_unsupported_script_does_not_write_file(self):
        with tempfile.TemporaryDirectory() as directory:
            script_path = Path(directory) / "ocs.user.js"
            for value in ("0", "-1", "invalid", str(main.MAX_OCS_ANSWER_TIMEOUT_SECONDS + 1)):
                with self.subTest(budget=value):
                    script_path.write_text(SCRIPT, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.patch_script(script_path, {"OCS_ANSWER_TIMEOUT_SECONDS": value})
                    self.assertEqual(script_path.read_text(encoding="utf-8"), SCRIPT)
            unsupported = SCRIPT.replace("AnswerWrapperHandlerConfig", "UnknownConfig")
            script_path.write_text(unsupported, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.patch_script(script_path)
            self.assertEqual(script_path.read_text(encoding="utf-8"), unsupported)
            for token in ("defaultAnswerWrapperHandler", "const responseData", "fet(url,"):
                with self.subTest(missing=token):
                    unsupported = SCRIPT.replace(token, "unsupported")
                    script_path.write_text(unsupported, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.patch_script(script_path)
                    self.assertEqual(script_path.read_text(encoding="utf-8"), unsupported)

    @unittest.skipUnless(JS_RUNTIME, "JavaScript runtime required for behavior check")
    def test_javascript_clamps_saved_settings_and_updates_both_timers(self):
        for budget in (main.DEFAULT_OCS_ANSWER_TIMEOUT_SECONDS, 1800):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as directory:
                script_path = Path(directory) / "ocs.user.js"
                script_path.write_text(SCRIPT, encoding="utf-8")
                self.patch_script(script_path)
                # Repatching must also allow changing the budget after a restart.
                self.patch_script(script_path, {"OCS_ANSWER_TIMEOUT_SECONDS": str(budget)})
                with script_path.open("a", encoding="utf-8") as script:
                    script.write("""
const values = [AnswerWrapperHandlerConfig.timeout_seconds];
for (const value of [120, 10000, NaN, Infinity, undefined]) {
  AnswerWrapperHandlerConfig.timeout_seconds = value;
  values.push(AnswerWrapperHandlerConfig.timeout_seconds);
}
let workerDelay;
globalThis.setTimeout = (callback, delay) => { workerDelay = delay; return 1; };
waitForRequested({});
const initialWorkerDelay = workerDelay;
AnswerWrapperHandlerConfig.timeout_seconds = Number.MAX_SAFE_INTEGER;
waitForRequested({});
console.log(JSON.stringify({values, workerDelay: initialWorkerDelay, maxWorkerDelay: workerDelay,
  setting: settings.answerWrapperHandlerTimeout}));
""")
                result = subprocess.run(
                    [JS_RUNTIME, str(script_path)], check=True, capture_output=True,
                    text=True, encoding="utf-8", timeout=30,
                )
                data = json.loads(result.stdout)
            self.assertEqual(data["values"], [budget, budget, 10000, budget, budget, budget])
            self.assertEqual(data["workerDelay"], (budget * 11 + 10 * 30 + 10) * 1000)
            self.assertEqual(data["maxWorkerDelay"], (main.MAX_OCS_ANSWER_TIMEOUT_SECONDS * 11 + 310) * 1000)
            self.assertLessEqual(data["maxWorkerDelay"], 2_147_483_647)
            self.assertEqual(data["setting"]["defaultValue"], budget)
            self.assertEqual(data["setting"]["attrs"]["min"], budget)
            self.assertGreaterEqual(data["setting"]["attrs"]["max"], budget)

    @unittest.skipUnless(JS_RUNTIME, "JavaScript runtime required for behavior check")
    def test_request_retries_and_failure_boundaries(self):
        scenarios = {
            "immediate_success": """
let calls = 0;
globalThis.fetch = async (url, options) => {
  calls++;
  assert.equal(url, wrapper.url);
  assert.equal(options.method, 'post');
  assert.deepEqual(options.data, wrapper.data);
  return ok();
};
assert.deepEqual(await settle(defaultAnswerWrapperHandler([wrapper], {})), [{results: ['题目', 'A']}]);
assert.equal(calls, 1);
assert.equal(now, 0);
""",
            "network_recovery": """
let calls = 0;
globalThis.fetch = async () => { if (++calls <= 2) throw new Error('offline'); return ok(); };
const result = await settle(defaultAnswerWrapperHandler([wrapper], {}));
assert.deepEqual(result, [{results: ['题目', 'A']}]);
assert.equal(calls, 3);
assert.equal(now, 60000);
""",
            "last_retry_succeeds": """
let calls = 0;
globalThis.fetch = async () => { if (++calls <= 10) throw new Error('offline'); return ok(); };
assert.deepEqual(await settle(defaultAnswerWrapperHandler([wrapper], {})), [{results: ['题目', 'A']}]);
assert.equal(calls, 11);
assert.equal(now, 300000);
""",
            "http_failure_exhausted": """
let calls = 0;
globalThis.fetch = async () => { calls++; return {ok: false, status: 502, json: async () => ({code: 0})}; };
const [result] = await settle(defaultAnswerWrapperHandler([wrapper], {}));
assert.match(result.error, /HTTP 502/);
assert.equal(calls, 11);
assert.equal(now, 300000);
// Non-bank requests retain the upstream response behavior.
assert.deepEqual(await settle(request(wrapper.url, {})), {code: 0});
assert.equal(calls, 12);
""",
            "timeouts_exhausted": """
let calls = 0;
globalThis.fetch = () => { calls++; return new Promise(() => {}); };
const [result] = await settle(defaultAnswerWrapperHandler([wrapper], {}));
assert.match(result.error, /题库请求超时/);
assert.equal(calls, 11);
assert.equal(now, (AnswerWrapperHandlerConfig.timeout_seconds * 11 + 300) * 1000);
""",
            "empty_answer_is_not_a_connection_error": """
let calls = 0;
globalThis.fetch = async () => { calls++; return ok({question: '题目'}); };
const [result] = await settle(defaultAnswerWrapperHandler([wrapper], {}));
assert.deepEqual(result.results, ['题目', undefined]);
assert.equal(calls, 1);
assert.equal(now, 0);
""",
            "handler_errors_do_not_retry": """
let calls = 0;
globalThis.fetch = async () => { calls++; return ok(); };
const badWrapper = {...wrapper, handler: "return () => { throw new Error('bad handler'); }"};
const [result] = await settle(defaultAnswerWrapperHandler([badWrapper], {}));
assert.equal(result.error, 'bad handler');
assert.equal(calls, 1);
assert.equal(now, 0);
""",
            "parallel_banks_retry_independently": """
const calls = {};
globalThis.fetch = async url => {
  calls[url] = (calls[url] || 0) + 1;
  if (url.includes('offline')) throw new Error('offline');
  return ok();
};
const offline = {...wrapper, url: 'https://offline.example/search'};
const result = await settle(defaultAnswerWrapperHandler([wrapper, offline], {}));
assert.deepEqual(result[0], {results: ['题目', 'A']});
assert.equal(result[1].error, 'offline');
assert.equal(calls[wrapper.url], 1);
assert.equal(calls[offline.url], 11);
assert.equal(now, 300000);
""",
        }
        with tempfile.TemporaryDirectory() as directory:
            script_path = Path(directory) / "ocs.user.cjs"
            script_path.write_text(SCRIPT, encoding="utf-8")
            self.patch_script(script_path)
            patched_script = script_path.read_text(encoding="utf-8")
            for name, scenario in scenarios.items():
                with self.subTest(scenario=name):
                    script_path.write_text(
                        patched_script + FAKE_CLOCK + "\n(async () => {\n" + scenario
                        + "\n})().catch(error => { console.error(error); process.exitCode = 1; });\n",
                        encoding="utf-8",
                    )
                    subprocess.run(
                        [JS_RUNTIME, str(script_path)], check=True, capture_output=True,
                        text=True, encoding="utf-8", timeout=30,
                    )


if __name__ == "__main__":
    unittest.main()
