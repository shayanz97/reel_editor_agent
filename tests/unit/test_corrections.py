import pytest
from pydantic import TypeAdapter

from reels.domain.corrections import (
    CorrectionPlan,
    DeleteSegment,
    DeleteWords,
    Highlight,
    KeepPause,
    MoveSubtitles,
    Note,
    ReplaceText,
    RestoreSegment,
    SplitSegment,
    Unhighlight,
    VersionState,
    apply,
    removed_segments,
    rerun_step,
    resolve,
)
from reels.domain.cutplan import CutParams, build_cut_plan
from reels.domain.segments import number_segments, split_sentences
from reels.pipeline.runner import STEP_ORDER
from tests.unit.helpers import words

#        1                  2                                   3                              4
TEXT = "Hallo zusammen. Heute geht es um Kubanetis. Das ist halt wichtig, halt. Tschüss."
WS = words(*[(t, i * 0.5, i * 0.5 + 0.4) for i, t in enumerate(TEXT.split())])
SPANS = split_sentences(WS)
ALL = {w.id for w in WS}


def _segments(state: VersionState):  # type: ignore[no-untyped-def]
    spans = split_sentences(WS, splits_after=state.splits_after)
    return number_segments(WS, spans, ALL - state.removed_words, state.overlay)


def _run(*ops, state: VersionState | None = None):  # type: ignore[no-untyped-def]
    state = state or VersionState()
    spans = split_sentences(WS, splits_after=state.splits_after)
    resolved, issues = resolve(
        list(ops), WS, _segments(state), removed_segments(spans, state.removed_words), state
    )
    return apply(resolved, WS, state), issues, resolved


def test_example_from_tasks_md_text_fix_and_delete() -> None:
    """„In 2 heißt es Kubernetes, Satz 3 raus“ -> exactly these two operations."""
    plan = CorrectionPlan(operations=[
        ReplaceText(op="replace_text", segment=2, find="Kubanetis", replace="Kubernetes"),
        DeleteSegment(op="delete_segment", segment=3),
    ])  # fmt: skip
    new, issues, _ = _run(*plan.operations)
    assert issues == []
    assert new.overlay == {6: "Kubernetes."}  # punctuation kept
    assert new.removed_words == {7, 8, 9, 10, 11}
    assert [s.text for s in _segments(new)] == [
        "Hallo zusammen.",
        "Heute geht es um Kubernetes.",
        "Tschüss.",
    ]
    assert rerun_step(plan.operations, STEP_ORDER) == "cutplan"


def test_plan_json_roundtrip_uses_discriminator() -> None:
    data = {"operations": [{"op": "note", "text": "Bitte Logo einblenden"}], "clarification": None}
    plan = TypeAdapter(CorrectionPlan).validate_python(data)
    assert isinstance(plan.operations[0], Note)
    with pytest.raises(ValueError):
        TypeAdapter(CorrectionPlan).validate_python({"operations": [{"op": "rm_rf", "path": "/"}]})


def test_replace_multi_word_phrase_hides_rest() -> None:
    new, issues, _ = _run(
        ReplaceText(op="replace_text", segment=1, find="hallo zusammen", replace="Servus")
    )
    assert issues == []
    assert new.overlay == {0: "Servus.", 1: ""}


def test_replace_inside_existing_overlay() -> None:
    state = VersionState(overlay={6: "Kubernetes."})
    new, issues, _ = _run(
        ReplaceText(op="replace_text", segment=2, find="Kubernetes", replace="K8s"), state=state
    )
    assert issues == [] and new.overlay[6] == "K8s."
    assert state.overlay[6] == "Kubernetes.", "base state is not modified"


def test_delete_and_restore_segment() -> None:
    deleted, _, _ = _run(DeleteSegment(op="delete_segment", segment=4))
    assert deleted.removed_words == {12}
    assert removed_segments(SPANS, deleted.removed_words)[0].first_word == 12
    restored, issues, _ = _run(RestoreSegment(op="restore_segment", removed=1), state=deleted)
    assert issues == [] and restored.removed_words == set()
    _, issues, _ = _run(RestoreSegment(op="restore_segment", removed=2), state=deleted)
    assert issues[0].kind == "removed_missing"


def test_delete_words() -> None:
    new, issues, _ = _run(DeleteWords(op="delete_words", segment=2, find="geht es"))
    assert issues == [] and new.removed_words == {3, 4}


def test_keep_pause_after_segment() -> None:
    new, issues, _ = _run(KeepPause(op="keep_pause", segment=1))
    assert issues == [] and new.keep_pauses_after == {1}


def test_highlight_and_unhighlight() -> None:
    state, issues, _ = _run(Highlight(op="highlight", segment=2, find="Kubanetis"))
    assert issues == [] and state.highlights == {6}
    state = state.model_copy(update={"highlights": {6, 0}})
    one, _, _ = _run(Unhighlight(op="unhighlight", segment=2, find="kubanetis"), state=state)
    assert one.highlights == {0}
    every, _, _ = _run(Unhighlight(op="unhighlight", segment=1, find=None), state=state)
    assert every.highlights == {6}


def test_move_subtitles() -> None:
    new, issues, _ = _run(MoveSubtitles(op="move_subtitles", position="upper_third"))
    assert issues == [] and new.subtitle == {"position": "upper_third"}


def test_split_segment_renumbers() -> None:
    new, issues, _ = _run(SplitSegment(op="split_segment", segment=2, after="geht es"))
    assert issues == [] and new.splits_after == {4}
    assert [s.text for s in _segments(new)][1:3] == ["Heute geht es", "um Kubanetis."]
    _, issues, _ = _run(SplitSegment(op="split_segment", segment=4, after="Tschüss"))
    assert issues[0].kind == "split_at_end"


def test_note_changes_nothing() -> None:
    new, issues, resolved = _run(Note(op="note", text="Logo bitte größer"))
    assert issues == [] and new == VersionState() and len(resolved) == 1
    assert rerun_step([r.op for r in resolved], STEP_ORDER) is None


@pytest.mark.parametrize(
    ("op", "kind"),
    [
        (DeleteSegment(op="delete_segment", segment=9), "segment_missing"),
        (ReplaceText(op="replace_text", segment=2, find="Docker", replace="X"), "find_missing"),
        (DeleteWords(op="delete_words", segment=3, find="halt"), "find_ambiguous"),
        (Highlight(op="highlight", segment=1, find="zusam"), "find_missing"),  # word boundaries
    ],
)
def test_invalid_references_become_issues(op, kind) -> None:  # type: ignore[no-untyped-def]
    new, issues, resolved = _run(op)
    assert [i.kind for i in issues] == [kind]
    assert resolved == [] and new == VersionState()


def test_empty_plan_asks_back() -> None:
    _, issues, _ = _run()
    assert [i.kind for i in issues] == ["nothing_to_do"]


def test_segment_numbers_refer_to_base_version() -> None:
    """After deleting segment 1, the old segment 3 is shown as 2; corrections use that."""
    base, _, _ = _run(DeleteSegment(op="delete_segment", segment=1))
    new, issues, _ = _run(DeleteWords(op="delete_words", segment=2, find="wichtig"), state=base)
    assert issues == [] and 10 in new.removed_words


@pytest.mark.parametrize(
    ("ops", "step"),
    [
        ([ReplaceText(op="replace_text", segment=1, find="a", replace="b")], "segments"),
        ([Highlight(op="highlight", segment=1, find="a")], "segments"),
        ([MoveSubtitles(op="move_subtitles", position="center")], "segments"),
        (
            [
                Highlight(op="highlight", segment=1, find="a"),
                DeleteSegment(op="delete_segment", segment=1),
            ],
            "cutplan",
        ),
    ],
)
def test_rerun_step(ops, step) -> None:  # type: ignore[no-untyped-def]
    assert rerun_step(ops, STEP_ORDER) == step


def test_keep_pause_in_cut_plan() -> None:
    ws = words(("a.", 0.0, 1.0), ("b", 2.0, 3.0))
    p = CutParams()
    assert len(build_cut_plan(ws, [], 3.0, p).keep) == 2
    kept = build_cut_plan(ws, [], 3.0, p, keep_pauses_after={0})
    assert len(kept.keep) == 1 and kept.output_duration == pytest.approx(3.0)
