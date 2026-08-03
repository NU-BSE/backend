from app.services.llm import (
    encode_event,
    run_error,
    run_finished,
    run_started,
    text_message_content,
    text_message_end,
    text_message_start,
    wire_messages_to_llm_messages,
)


def test_wire_messages_flatten_text_turns():
    messages = [
        {"role": "system", "content": "be spooky"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "I see you"},
        {"role": "tool", "content": "noise"},
        {"role": "reasoning", "content": "inner monologue"},
        {"role": "user", "content": [{"type": "text", "text": "multimodal "}, {"type": "image"}]},
        {"role": "user", "content": "   "},
    ]
    flat = wire_messages_to_llm_messages(messages)
    assert flat == [
        {"role": "system", "content": "be spooky"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "I see you"},
        {"role": "user", "content": "multimodal"},
    ]


def test_wire_messages_ignores_garbage():
    assert wire_messages_to_llm_messages([None, 42, {"role": "user"}]) == []


def test_event_shapes_match_agui():
    started = run_started("t1", "r1", "model-x")
    assert started["type"] == "RUN_STARTED"
    assert started["threadId"] == "t1"
    assert started["runId"] == "r1"
    assert "timestamp" in started

    start = text_message_start("m1", "model-x")
    assert start["type"] == "TEXT_MESSAGE_START"
    assert start["messageId"] == "m1"
    assert start["role"] == "assistant"

    delta = text_message_content("m1", "boo")
    assert delta["type"] == "TEXT_MESSAGE_CONTENT"
    assert delta["delta"] == "boo"

    end = text_message_end("m1")
    assert end["type"] == "TEXT_MESSAGE_END"

    finished = run_finished("t1", "r1", "model-x")
    assert finished["type"] == "RUN_FINISHED"

    error = run_error("boom", "UPSTREAM_ERROR")
    assert error["type"] == "RUN_ERROR"
    assert error["message"] == "boom"
    assert error["code"] == "UPSTREAM_ERROR"


def test_encode_event_is_ndjson_line():
    line = encode_event(run_started("t", "r", "m"))
    assert line.endswith(b"\n")
    assert b"\n" not in line[:-1]
