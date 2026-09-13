"""scripts/ 回归测试（unittest，纯标准库）。

跑法（在技能根目录）：
    python3 -m unittest scripts.tests.test_scripts -v

覆盖历次审查实锤修复的回归：超时容错、多行 description、异常保 history、
state.db 判定路径、锚点 resolve、eval_id 回退。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _tempdir(tc: unittest.TestCase) -> Path:
    """mkdtemp + 统一注册清理，测试不再泄漏 /tmp 目录。"""
    td = Path(tempfile.mkdtemp())
    tc.addCleanup(shutil.rmtree, td, True)
    return td


class TestGradeRunsTimeout(unittest.TestCase):
    """grade_runs：单个 check.py 挂起不得炸掉整轮批量评分。"""

    def test_hanging_check_returns_error_not_raise(self):
        from scripts import grade_runs

        td = _tempdir(self)
        check = td / "check.py"
        check.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        outputs = td / "outputs"
        outputs.mkdir()
        status, data, diag = grade_runs.run_check(check, outputs, timeout=1)
        self.assertEqual(status, "timeout")
        self.assertIsNone(data)
        self.assertIn("超时", diag)


class TestGenReviewPageDescription(unittest.TestCase):
    """gen_review_page：多行 description 不得丢成 '|'。"""

    def test_multiline_description_extracted(self):
        from scripts import gen_review_page

        td = _tempdir(self) / "demo-skill"
        td.mkdir()
        (td / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: |\n  第一行描述\n  第二行继续\n---\n\n# demo-skill\n",
            encoding="utf-8")
        name, desc = gen_review_page.load_skill_info(td)
        self.assertEqual(name, "demo-skill")
        self.assertIn("第二行继续", desc)
        self.assertNotEqual(desc.strip(), "|")


class TestRunLoopHistorySurvival(unittest.TestCase):
    """run_loop：执行中抛异常时已完成迭代的 history 不得丢。"""

    def _make_skill(self, td: Path) -> Path:
        skill = td / "sk"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: sk\ndescription: d\n---\n\n# sk\n\n## When to Use\n\nx\n",
            encoding="utf-8")
        return skill

    def test_history_ref_populated_before_exception(self):
        from scripts import run_loop

        td = _tempdir(self)
        skill = self._make_skill(td)
        calls = {"n": 0}

        def fake_run_eval(**kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("boom on iter 2")
            # 第一轮有失败 → 不会 all_passed 提前退出，会进 improve → 第二轮
            return {
                "results": [{"query": "q", "should_trigger": True, "trigger_rate": 0.0,
                             "triggers": 0, "runs": 1, "pass": False}],
                "summary": {"total": 1, "passed": 0, "failed": 1},
            }

        history_ref: list = []
        with mock.patch.object(run_loop, "run_eval", side_effect=fake_run_eval), \
             mock.patch.object(run_loop, "improve_description", return_value="new d"):
            with self.assertRaises(RuntimeError):
                run_loop.run_loop(
                    eval_set=[{"query": "q", "should_trigger": True}],
                    skill_path=skill,
                    description_override=None,
                    num_workers=1,
                    timeout=10,
                    max_iterations=3,
                    runs_per_query=1,
                    trigger_threshold=0.5,
                    holdout=0,
                    model="m",
                    verbose=False,
                    history_ref=history_ref,
                )
        self.assertEqual(len(history_ref), 1)
        self.assertEqual(history_ref[0]["train_passed"], 0)  # 第一轮失败也照常入档
        self.assertEqual(history_ref[0]["train_results"][0]["query"], "q")


class TestSessionViewedSkill(unittest.TestCase):
    """run_eval：请求参数存于 assistant 行 tool_calls JSON 的 arguments 里。"""

    def _db(self, td: Path) -> Path:
        db = td / "state.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
        con.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp)"
            " VALUES ('s1', 'assistant', '', ?, NULL, 1.0)",
            ('[{"id": "c1", "type": "function", "function": {"name": "skill_view",'
             ' "arguments": "{\\"name\\":\\"sk-eval-ab12\\"}"}}]',))
        con.commit()
        con.close()
        return db

    def test_arguments_path_hits(self):
        from scripts import run_eval

        td = _tempdir(self)
        db = self._db(td)
        self.assertTrue(run_eval._session_viewed_skill("s1", "sk-eval-ab12", db_path=db))
        self.assertFalse(run_eval._session_viewed_skill("s1", "other-skill", db_path=db))
        self.assertFalse(run_eval._session_viewed_skill("nobody", "sk-eval-ab12", db_path=db))

    def test_fixture_name_only_attribution(self):
        """归因口径：skill_view 的若非夹具名（如真技能名），不得判触发。"""
        from scripts import run_eval

        td = _tempdir(self)
        db = self._db(td)  # 唯一调用对象是 sk-eval-ab12
        # 夹具名命中
        self.assertTrue(run_eval._session_viewed_skill("s1", "sk-eval-ab12", db_path=db))
        # 真技能名（sk）不是夹具名——即便会话真 view 了已装副本也不算候选触发
        self.assertFalse(run_eval._session_viewed_skill("s1", "sk", db_path=db))


class TestSkillViewHitChecksName(unittest.TestCase):
    """aggregate：with_skill 真用检查须核对调用对象＝被测技能，读别的技能不算。"""

    def _db(self, td: Path) -> Path:
        db = td / "state.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
        # 执行器 skill_view 了别的技能（非被测技能 sk）——不得算真用
        con.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp)"
            " VALUES ('s1', 'assistant', '', ?, NULL, 1.0)",
            ('[{"id": "c1", "type": "function", "function": {"name": "skill_view",'
             ' "arguments": "{\\\"name\\\":\\\"other-skill\\\"}"}}]',))
        con.commit()
        con.close()
        return db

    def test_other_skill_view_not_counted(self):
        from scripts import aggregate_benchmark

        td = _tempdir(self)
        db = self._db(td)
        con = sqlite3.connect(db)
        self.assertFalse(aggregate_benchmark._skill_view_hit(con, "s1", "sk"))
        self.assertTrue(aggregate_benchmark._skill_view_hit(con, "s1", "other-skill"))
        con.close()


class TestVerifyRunsAnchorResolve(unittest.TestCase):
    """aggregate：_verify_runs 锚点须 resolve，且不得被 '_' 通配符误配。"""

    def _db(self, td: Path) -> Path:
        db = td / "state.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
        # 真写者：消息里含规范路径 /private/tmp/wsx/eval-a/with_skill/run-1/outputs
        con.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, timestamp)"
            " VALUES ('writer', 'assistant', '写到 /private/tmp/wsx/eval-a/with_skill/run-1/outputs/r.md', NULL, 1.0)")
        # 干扰项：结构相近路径（withXskill）不得命中 with_skill 的 LIKE
        con.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, timestamp)"
            " VALUES ('noise', 'assistant', '提到 /private/tmp/wsx/eval-a/withXskill/run-1/outputs', NULL, 1.0)")
        con.commit()
        con.close()
        return db

    def test_symlinkish_path_finds_writer(self):
        from scripts import aggregate_benchmark

        # 锚点构造：未 resolve 的路径形态（macOS /tmp→/private/tmp symlink）
        # 也必须产出规范形态锚点，LIKE 才能命中消息里的规范路径
        anchors = aggregate_benchmark._writer_anchors(
            Path("/tmp/wsx/eval-a/with_skill/run-1/outputs"))
        resolved = str(Path("/tmp/wsx/eval-a/with_skill/run-1/outputs").resolve())
        self.assertIn(resolved, anchors)

        # LIKE 转义：锚点里的 '_' 不得当通配符用
        con_like = aggregate_benchmark._escape_like("/private/tmp/wsx/eval-a/with_skill/run-1/outputs")
        self.assertNotIn("_", con_like.replace("\\_", ""))
        # 转义后 LIKE 'withXskill' 不得命中 'with_skill' 的模式
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE t (s TEXT)")
        db.execute("INSERT INTO t VALUES ('/private/tmp/wsx/eval-a/withXskill/run-1/outputs')")
        n = db.execute(
            "SELECT COUNT(*) FROM t WHERE s LIKE ? ESCAPE '\\'",
            ("%" + aggregate_benchmark._escape_like(
                str(Path("/tmp/wsx/eval-a/with_skill/run-1/outputs").resolve())) + "%",)).fetchone()[0]
        self.assertEqual(n, 0)  # withXskill 不得被 with_skill 锚点命中
        db.close()


class TestTokensFromSessionUsage(unittest.TestCase):
    """aggregate：state.db-session 兜底——中文路径 \\uXXXX 转义匹配＋多写入会话取小。"""

    def test_chinese_path_and_min_session(self):
        from scripts import aggregate_benchmark

        td = _tempdir(self)
        outputs = td / "eval-检验范围" / "with_skill" / "run-1" / "outputs"
        outputs.mkdir(parents=True)
        db = td / "state.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
        # 真写者：tool_calls JSON 里中文以 \uXXXX 转义存储（与 Hermes state.db 同形态——
        # arguments 为字符串套 JSON 会双重转义，真实库是单层，构造后折叠对齐）
        import json as _json
        calls = [{"function": {"name": "write_file", "arguments": _json.dumps(
            {"path": f"{outputs.as_posix()}/answer.md"}, ensure_ascii=True)}}]
        stored = _json.dumps(calls).replace("\\\\u", "\\u")
        con.execute(
            "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?,?,?,1.0)",
            ("sub-agent", "assistant", stored))
        # 干扰项：主会话也提到该路径（content 明文，非 tool_calls 列）不得计入
        con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,1.0)",
            ("main-session", "assistant", f"查看 {outputs.as_posix()}/answer.md"))
        con.execute(
            "CREATE TABLE session_model_usage (session_id TEXT, model TEXT, api_call_count INTEGER,"
            " input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,"
            " reasoning_tokens INTEGER)")
        con.execute(
            "INSERT INTO session_model_usage VALUES ('sub-agent','m',3,20160,3395,53824,1296)")
        con.execute(
            "INSERT INTO session_model_usage VALUES ('main-session','m',130,100,100,0,0)")
        con.commit()
        con.close()

        r = aggregate_benchmark._tokens_from_session_usage(outputs.parent, db)
        self.assertIsNotNone(r)
        self.assertEqual(r["source"], "state.db-session")
        self.assertEqual(r["total_tokens"], 20160 + 3395 + 53824)
        self.assertEqual(r["api_calls"], 3)


class TestPassRateFallback(unittest.TestCase):
    """aggregate：grading.json 的 summary 缺 pass_rate 时由 passed/total 现算，不得静默 0。"""

    def test_missing_pass_rate_computed(self):
        from scripts import aggregate_benchmark

        td = _tempdir(self)
        for cfg in ("with_skill", "without_skill"):
            rd = td / "eval-1" / cfg / "run-1"
            rd.mkdir(parents=True)
            (rd / "grading.json").write_text(json.dumps(
                {"summary": {"passed": 4, "failed": 1, "total": 5}}),  # 无 pass_rate
                encoding="utf-8")
        res = aggregate_benchmark.load_run_results(td)
        self.assertAlmostEqual(res["with_skill"][0]["pass_rate"], 0.8)
        self.assertAlmostEqual(res["without_skill"][0]["pass_rate"], 0.8)


class TestEvalIdFallbackNone(unittest.TestCase):
    """aggregate：eval_id 回退不得用 0 基序号与 1 基编号碰撞。"""

    def test_broken_metadata_falls_back_to_none(self):
        from scripts import aggregate_benchmark

        td = _tempdir(self)
        # eval-1：正常目录名回退得 eval_id=1
        for cfg in ("without_skill",):
            rd = td / "eval-1" / cfg / "run-1"
            rd.mkdir(parents=True)
            (rd / "grading.json").write_text(json.dumps(
                {"summary": {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0}}),
                encoding="utf-8")
        # eval-描述名：metadata 损坏 → 应回退 None（不得=1 与 eval-1 碰撞）
        rd2 = td / "eval-格式转换" / "without_skill" / "run-1"
        rd2.mkdir(parents=True)
        (rd2 / "grading.json").write_text(json.dumps(
            {"summary": {"passed": 0, "failed": 1, "total": 1, "pass_rate": 0.0}}),
            encoding="utf-8")
        (td / "eval-格式转换" / "eval_metadata.json").write_text("{broken json", encoding="utf-8")

        res = aggregate_benchmark.load_run_results(td)
        ids = {(r["eval_id"], r["eval_name"]) for r in res["without_skill"]}
        id_values = {i for i, _ in ids}
        self.assertIn(None, id_values)  # 损坏者回退 None
        self.assertIn(1, id_values)     # eval-1 正常
        self.assertNotIn(0, id_values)  # 不得出现 0 基碰撞值


class TestTableColumnsQuotedComma(unittest.TestCase):
    """table_columns 须按 CSV 语义数列——引号内的逗号不算分隔符。"""

    def test_quoted_comma_field(self):
        from scripts.check_common import table_columns
        td = _tempdir(self)
        csv_path = td / "t.csv"
        csv_path.write_text('姓名,备注\n张三,"部门A, 组B"\n', encoding="utf-8")
        self.assertTrue(table_columns(csv_path, 2))

    def test_plain_still_works(self):
        from scripts.check_common import table_columns
        td = _tempdir(self)
        csv_path = td / "t.csv"
        csv_path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        self.assertTrue(table_columns(csv_path, 3))
        self.assertFalse(table_columns(csv_path, 2))

    def test_custom_delimiter_with_quote(self):
        from scripts.check_common import table_columns
        td = _tempdir(self)
        csv_path = td / "t.txt"
        csv_path.write_text('a;b\n"x;y";2\n', encoding="utf-8")
        self.assertTrue(table_columns(csv_path, 2, delimiter=";"))


class TestFrontmatterParserEdges(unittest.TestCase):
    """utils 手写 YAML 子集解析器的边界（评审实锤回归）。"""

    def test_list_item_blank_line(self):
        from scripts.utils import parse_frontmatter
        r = parse_frontmatter(
            "metadata:\n  hermes:\n    config:\n      - key: a\n        value: b\n\n      - key: c\n        value: d\n")
        items = r["metadata"]["hermes"]["config"]
        self.assertEqual(items, [{"key": "a", "value": "b"}, {"key": "c", "value": "d"}])

    def test_inline_list_quoted_comma(self):
        from scripts.utils import parse_frontmatter
        r = parse_frontmatter('platforms: ["a, b", "c"]')
        self.assertEqual(r["platforms"], ["a, b", "c"])

    def test_block_scalar_indent_base(self):
        from scripts.utils import parse_frontmatter
        r = parse_frontmatter("description: |\n      第一行\n      第二行\n")
        self.assertEqual(r["description"], "第一行\n第二行\n")

    def test_tab_indent_rejected(self):
        from scripts.utils import parse_frontmatter
        # docstring 承诺超出子集抛 ValueError——tab 缩进必须显式报错而不是静默压平
        with self.assertRaises(ValueError):
            parse_frontmatter("metadata:\n\thermes:\n\t\ttags: [a]\n")

    def test_unicode_superscript_digit_not_number(self):
        from scripts.utils import parse_frontmatter
        # '²'.isdigit() 为 True 但 int() 会炸——非 ASCII 数字须按字符串收
        r = parse_frontmatter("note: 信号²")
        self.assertEqual(r["note"], "信号²")

    def test_bare_url_list_item_stays_scalar(self):
        from scripts.utils import parse_frontmatter
        # 裸 URL 里的冒号后无空格，不是 mapping 项（与 yaml.safe_load 语义一致）
        r = parse_frontmatter("refs:\n  - https://example.com/a\n")
        self.assertEqual(r["refs"], ["https://example.com/a"])

    def test_nested_mapping_in_list_item_rejected(self):
        from scripts.utils import parse_frontmatter
        # 字段值本身又是 mapping 超出子集——必须显式报错，不得静默压扁丢数据
        with self.assertRaises(ValueError):
            parse_frontmatter("items:\n  - key: a\n    deep:\n      k: v\n")

    def test_trailing_comma_inline_list(self):
        from scripts.utils import parse_frontmatter
        r = parse_frontmatter("platforms: [a, b,]")
        self.assertEqual(r["platforms"], ["a", "b"])


class TestQuickValidateH1(unittest.TestCase):
    """strict 的 H1 检查不得被代码块内 '# ' 注释劫持。"""

    def test_code_fence_before_h1(self):
        from scripts.quick_validate import validate_strict
        td = _tempdir(self) / "demo-skill"
        td.mkdir()
        (td / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: d\n---\n\n```bash\n# 这是代码块注释\n```\n\n"
            "# demo-skill\n\n## When to Use\n\nx\n\n## Procedure\n\ny\n\n## Pitfalls\n\nz\n\n## Verification\n\nw\n",
            encoding="utf-8")
        ok, msg = validate_strict(td)
        self.assertTrue(ok, msg)


class TestGenReviewPageScriptEscape(unittest.TestCase):
    """gen_review_page：嵌入数据含 </script> 不得截断脚本块；占位符字样不得二次注入。"""

    def test_malicious_query_data_escaped(self):
        from scripts import gen_review_page

        td = _tempdir(self)
        q = td / "q.json"
        q.write_text(json.dumps([
            {"query": "</script><script>alert(1)</script>", "should_trigger": True},
            {"query": "__SKILL_NAME_PLACEHOLDER__", "should_trigger": False},
        ]), encoding="utf-8")
        skill = td / "evil-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: evil-skill\ndescription: d\n---\n\n# evil-skill\n", encoding="utf-8")
        html_text = gen_review_page.build_page(
            queries=[{"query": "</script><script>x</script>", "should_trigger": True},
                     {"query": "__SKILL_NAME_PLACEHOLDER__", "should_trigger": False}],
            skill_path=skill,
        )
        self.assertNotIn("</script><script>alert", html_text)
        # 数据里含占位符字样时，name 替换不得命中数据位置（EVAL_DATA 最后替换）
        self.assertNotIn("__EVAL_DATA_PLACEHOLDER__", html_text)
        # name 占位符字样出现在数据里时必须原样保留（不被技能名污染）
        self.assertIn("__SKILL_NAME_PLACEHOLDER__", html_text.split("const EVAL_DATA", 1)[1])


class TestRunEvalArgBounds(unittest.TestCase):
    """run_eval：--num-workers/--runs-per-query 无下界时的裸 traceback 须变成可读报错。"""

    def test_zero_workers_rejected(self):
        from scripts import run_eval

        with mock.patch.object(sys, "argv", ["run_eval", "--num-workers", "0",
                                             "--eval-set", "x", "--skill-path", "y"]):
            with self.assertRaises(SystemExit) as cm:
                run_eval.main()
            self.assertNotEqual(cm.exception.code, 0)


class TestPrecheckGhostPath(unittest.TestCase):
    """幽灵路径：URL 内子串不得误报；仓库相对引用前须无字词边界外字符。"""

    def test_url_substring_no_ghost(self):
        import subprocess
        td = _tempdir(self) / "demo2"
        td.mkdir()
        (td / "SKILL.md").write_text(
            "---\nname: demo2\ndescription: d\n---\n\n# demo2\n\n## When to Use\n\nx\n\n"
            "## Procedure\n\n参考 https://github.com/foo/bar/scripts/check.py 文档\n\n"
            "## Pitfalls\n\ny\n\n## Verification\n\nz\n",
            encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "scripts.precheck_deliver", str(td)],
                           capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
        self.assertNotIn("幽灵路径", r.stderr)

    def test_chinese_ref_still_detected(self):
        import subprocess
        td = _tempdir(self) / "demo3"
        td.mkdir()
        (td / "references").mkdir()
        (td / "references" / "存在的指南.md").write_text("占位", encoding="utf-8")
        (td / "SKILL.md").write_text(
            "---\nname: demo3\ndescription: d\n---\n\n# demo3\n\n## When to Use\n\nx\n\n"
            "## Procedure\n\n见 references/不存在的中文名.md\n\n## Pitfalls\n\ny\n\n## Verification\n\nz\n",
            encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "scripts.precheck_deliver", str(td)],
                           capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
        self.assertIn("幽灵路径", r.stderr)


if __name__ == "__main__":
    unittest.main()
