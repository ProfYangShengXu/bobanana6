"""长产物写作三条款：提示要点名 `files.read`、分开「要算的 / 要写的」、长产物分次 append。

现场（`acceptance/ab/agent-jd-research/`，4B 模型；痕迹树里的三条读数）：

```
① T-10 一块连着 11 次 `shell.run` 硬读自己刚写的文件 —— 每一次都 `ok=True`，
   而 `repeat_block_after` / `same_tool_block_after` 只数【失败】调用 ⇒ 一条都没拦；
② 系统提示要「回读确认」，可当时的工具面里没有回读工具，模型只能 `cat`；
③ produce.py 那句「要算的东西先写成脚本」被 4B 用到【文字内容】上
   ⇒ 产物模板化（脚本循环把同一个值填进每一行）。
```

本文件钉住三处提示文字，并且证明它们真的到了模型手上（驱动真链路取 `messages[0]`）：

```
① 回读那节点名 `files.read`，并说清它读的是「你有权写的那些文件」；
② 区分「要【算】的」与「要【写】的」，明说文字内容不用脚本生成；
③ 长产物分次 `append=true` 续写 + 用 `files.read` 带 `offset` 回读最后一段。
★ 反向（本文件的核心）：把任一条从提示里删掉 ⇒ 对应断言必须红。
★ 领域无关：这些是【数据】，断言里不许出现本题（JD / 薪资 / 攻略这类）的词。
```

★ 断言一律**按小节定位**（`_section`），不按整条 system 里散落的 token：
同一段 system 里「files.write + shell.run」「不要内联一大段代码」出现过不止一次，
按整条做 token 断言会让「删掉新串」仍然命中旧处 ⇒ 反向测试假绿。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from acceptance.harness import run_task
from bobanana6.assemble_impl import produce
from bobanana6.core.model import ModelResponse
from tests.test_convert_three import _task

#: 三处新增/改写的 `##` 小节标题（提示里唯一，用作定位锚点）。
_READBACK = "写在文件里的，要回读确认"
_COMPUTE_WRITE = "要算的写成脚本，要写的直接写"
_LONG_APPEND = "产物长就分几次写"


# --------------------------------------------------------------------------- #
# 真链路取提示：system 到底送出了什么
# --------------------------------------------------------------------------- #


def _system_prompt(task_dir: Path) -> str:
    """驱动一次真装配（假模型，零网络），把模型收到的 `messages[0]` 原样取回。"""
    systems: list[str] = []

    class _Client:
        def complete(self, messages, tools, max_tokens, **kw):  # type: ignore[no-untyped-def]
            if not systems:
                systems.append(str(messages[0].get("content", "")))
            return ModelResponse(
                text=json.dumps(
                    {
                        "ref": "seed",
                        "columns": [{"name": "obligor", "dtype": "str"}],
                        "rows": [{"obligor": "A-1"}],
                        "done": True,
                    }
                )
            )

    run_task(task_dir, client=_Client(), env={}, workspace=True)
    assert systems, "the run must have made at least one model call"
    return systems[0]


@pytest.fixture(scope="module")
def real_prompt(tmp_path_factory: pytest.TempPathFactory) -> str:
    """真提示只取一次（模块级缓存）：正向断言与领域无关断言共用同一份。"""
    task = _task(tmp_path_factory.mktemp("volume-prompt"), name="volume")
    return _system_prompt(task)


def _section(prompt: str, header: str) -> str:
    """`## <header>` 到下一个 `## ` 之间的正文；找不到就返回空串。"""
    marker = f"## {header}"
    if marker not in prompt:
        return ""
    body = prompt.split(marker, 1)[1]
    return body.split("\n## ", 1)[0]


def _strand_checks(prompt: str) -> dict[str, bool]:
    """三条各自的独立判据 —— 每条要求多枚该小节【独有】的标记同时在场。"""
    readback = _section(prompt, _READBACK)
    compute_write = _section(prompt, _COMPUTE_WRITE)
    long_append = _section(prompt, _LONG_APPEND)
    return {
        # ① 回读那节点名 files.read，并划清与 sources.read 的边界
        "readback-names-files.read": (
            "`files.read`" in readback
            and "有权写的那些文件" in readback
            and "`sources.read`" in readback
        ),
        # ② 要算的 / 要写的分开，且明说文字不能用脚本生成
        "compute-vs-write": (
            "要【算】" in compute_write
            and "要【写】" in compute_write
            and "生成这段文字的脚本" in compute_write
            and "不用于产出文字本身" in compute_write
            and "循环填出来的" in compute_write
        ),
        # ③ 长产物分次 append + 带 offset 回读最后一段、别靠记忆接
        "long-append": (
            "`append=true`" in long_append
            and "第 1 次 `files.write`" in long_append
            and "`files.read`" in long_append
            and "`offset`" in long_append
            and "别靠记忆接" in long_append
        ),
    }


# --------------------------------------------------------------------------- #
# 正向：三条都在【真的送到模型手上】的 system 里
# --------------------------------------------------------------------------- #


def test_the_edited_guidance_is_delivered_in_the_system_message(real_prompt: str) -> None:
    """先证明送达：我们改的那个常量逐字出现在模型收到的 `messages[0]` 里。"""
    assert produce.EXECUTION_DISCIPLINE in real_prompt


def test_the_readback_rule_names_files_read(real_prompt: str) -> None:
    """① 回读那节点名 `files.read`，且说清它读的是自己有权写的文件。"""
    checks = _strand_checks(real_prompt)
    assert checks["readback-names-files.read"] is True, checks


def test_the_guidance_separates_computing_from_writing(real_prompt: str) -> None:
    """② 明确区分「要算的」（脚本）与「要写的」（直接写），文字不许脚本生成。"""
    checks = _strand_checks(real_prompt)
    assert checks["compute-vs-write"] is True, checks


def test_the_guidance_says_long_artifacts_are_written_in_appends(real_prompt: str) -> None:
    """③ 长产物分次 append，并用 `files.read` 带 offset 回读最后一段接力。"""
    checks = _strand_checks(real_prompt)
    assert checks["long-append"] is True, checks


# --------------------------------------------------------------------------- #
# 领域无关：提示是数据，不许写进本题的词
# --------------------------------------------------------------------------- #

#: ★ 词表从目标题（`acceptance/ab/agent-jd-research/task.yaml`）的 goal / deliverables 里抽，
#: 只留【歧义最小】的词 —— 例如「字节」不能用：它在源码里是「逐字节相同」的 byte。
#: 题面 / goal 是任务数据，由契约注入 system；这里断言的是**我们自己写的恒定引导**。
DOMAIN_WORDS = (
    "JD",
    "薪资",
    "攻略",
    "面试",
    "招聘",
    "面经",
    "大厂",
    "腾讯",
    "阿里巴巴",
    "百度",
    "美团",
    "快手",
    "小红书",
    "华为",
    "微软",
    "OpenAI",
    "jd-matrix",
    "interview-bank",
    "roadmap",
    "简历",
    "求职",
    "字节跳动",
)


def test_the_static_guidance_carries_no_task_domain_word(real_prompt: str) -> None:
    """恒定引导（逐块同文）里不许出现本题的词；否则就是过拟合在这道题上。

    ★ 断言范围是**我们写的恒定引导三块**（`BLOCK_COMPLETION_GUIDANCE` /
    `TOOL_USE_ENFORCEMENT_GUIDANCE` / `EXECUTION_DISCIPLINE`），不是整条 system：
    `contract.goal` / `statement` / `source_profiles` 是契约注入的**数据**，
    它们本来就含领域词（`suspicious #656`）。送达由上面的 `real_prompt` 断言保证。
    """
    static = (
        produce.BLOCK_COMPLETION_GUIDANCE
        + produce.TOOL_USE_ENFORCEMENT_GUIDANCE
        + produce.EXECUTION_DISCIPLINE
    )
    hits = [word for word in DOMAIN_WORDS if word in static]
    assert hits == [], f"static guidance leaked task-domain words: {hits}"
    # 同一份引导确实在真提示里（领域无关断言不是对着一个没人用的常量空转）。
    assert produce.EXECUTION_DISCIPLINE in real_prompt


# --------------------------------------------------------------------------- #
# 反向：把任一条从提示里删掉 ⇒ 对应断言必须红
# --------------------------------------------------------------------------- #


def _drop_section(text: str, header: str) -> str:
    """删掉 `## <header>` 整节（到下一个 `## ` 之前）。"""
    marker = f"## {header}"
    start = text.index(marker)
    try:
        end = text.index("\n## ", start)
    except ValueError:
        return text[:start]
    return text[:start] + text[end + 1 :]


def _strip_readback_files_read(text: str) -> str:
    """只删「点名 `files.read`」这一条：留下小节骨架，把它换成不含该工具的写法。"""
    section = _section(text, _READBACK)
    assert section, "the readback section must exist for this mutation"
    mutated = section.replace("`files.read`", "`某个读工具`")
    assert mutated != section, "the readback section must actually name files.read"
    return text.replace(section, mutated)


def _strip(text: str, strand: str) -> str:
    if strand == "readback-names-files.read":
        return _strip_readback_files_read(text)
    if strand == "compute-vs-write":
        return _drop_section(text, _COMPUTE_WRITE)
    if strand == "long-append":
        return _drop_section(text, _LONG_APPEND)
    raise AssertionError(f"unknown strand {strand!r}")


#: 三条的稳定名字（与 `_strand_checks` 的键一一对应；多一条少一条在这里立刻红）。
_STRANDS = ("readback-names-files.read", "compute-vs-write", "long-append")


def test_the_strand_table_matches_the_checks() -> None:
    assert tuple(sorted(_STRANDS)) == tuple(sorted(_strand_checks("")))


@pytest.mark.parametrize("strand", _STRANDS)
def test_deleting_a_strand_turns_its_check_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strand: str
) -> None:
    """★ 反向：只把这一条从提示里删掉 ⇒ 它的断言变红，另外两条仍然绿。

    证明测试钉的是【这三条本身】，不是「提示里恰好还有别的同义句子」。
    """
    original = produce.EXECUTION_DISCIPLINE
    mutated = _strip(original, strand)
    assert mutated != original, "the mutation must actually change the prompt"
    monkeypatch.setattr(produce, "EXECUTION_DISCIPLINE", mutated)

    prompt = _system_prompt(_task(tmp_path, name=f"drop-{strand}"))
    checks = _strand_checks(prompt)
    assert checks[strand] is False, f"deleting {strand!r} left its check green: {checks}"
    others = [name for name, ok in checks.items() if name != strand and not ok]
    assert others == [], f"removing {strand!r} also broke {others}: {checks}"
