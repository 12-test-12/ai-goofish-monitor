import asyncio

import pytest

import src.prompt_utils as prompt_utils
from src.services.ai_response_parser import EmptyAIResponseError


def test_generate_criteria_closes_ai_client_after_success(monkeypatch, tmp_path):
    close_state = {"closed": False}
    reference_file = tmp_path / "reference.txt"
    reference_file.write_text("reference", encoding="utf-8")

    class FakeAIClient:
        def is_available(self):
            return True

        def refresh(self):
            raise AssertionError("refresh should not be called")

        async def _call_ai(self, *_args, **_kwargs):
            return "generated criteria"

        async def close(self):
            close_state["closed"] = True

    monkeypatch.setattr(prompt_utils, "AIClient", FakeAIClient)

    result = asyncio.run(
        prompt_utils.generate_criteria("need a gpu", str(reference_file))
    )

    assert result == "generated criteria"
    assert close_state["closed"] is True


def test_generate_criteria_drops_think_blocks_and_raises_output_limit(monkeypatch, tmp_path):
    reference_file = tmp_path / "reference.txt"
    reference_file.write_text("reference", encoding="utf-8")
    captured = {}

    class FakeAIClient:
        def is_available(self):
            return True

        def refresh(self):
            raise AssertionError("refresh should not be called")

        async def _call_ai(self, *_args, **kwargs):
            captured.update(kwargs)
            return "<think>先分析需求</think>\n\n一票否决：电池健康度不足"

        async def close(self):
            return None

    monkeypatch.setattr(prompt_utils, "AIClient", FakeAIClient)

    result = asyncio.run(
        prompt_utils.generate_criteria("need a watch", str(reference_file))
    )

    assert result == "一票否决：电池健康度不足"
    assert captured["max_output_tokens"] == prompt_utils.CRITERIA_MAX_OUTPUT_TOKENS
    assert captured["max_output_tokens"] > 800


def test_generate_criteria_rejects_truncated_think_block(monkeypatch, tmp_path):
    reference_file = tmp_path / "reference.txt"
    reference_file.write_text("reference", encoding="utf-8")

    class FakeAIClient:
        def is_available(self):
            return True

        def refresh(self):
            raise AssertionError("refresh should not be called")

        async def _call_ai(self, *_args, **_kwargs):
            return "<think>输出到这里被截断"

        async def close(self):
            return None

    monkeypatch.setattr(prompt_utils, "AIClient", FakeAIClient)

    with pytest.raises(RuntimeError, match="思考过程占满了输出长度"):
        asyncio.run(prompt_utils.generate_criteria("need a watch", str(reference_file)))


def test_generate_criteria_closes_ai_client_after_ai_failure(monkeypatch, tmp_path):
    close_state = {"closed": False}
    reference_file = tmp_path / "reference.txt"
    reference_file.write_text("reference", encoding="utf-8")

    class FakeAIClient:
        def is_available(self):
            return True

        def refresh(self):
            raise AssertionError("refresh should not be called")

        async def _call_ai(self, *_args, **_kwargs):
            raise EmptyAIResponseError("AI响应内容为空。")

        async def close(self):
            close_state["closed"] = True

    monkeypatch.setattr(prompt_utils, "AIClient", FakeAIClient)

    with pytest.raises(EmptyAIResponseError, match="AI响应内容为空"):
        asyncio.run(prompt_utils.generate_criteria("need a gpu", str(reference_file)))

    assert close_state["closed"] is True


def test_slow_criteria_generation_times_out_cancels_request_and_closes_client(monkeypatch, tmp_path):
    events = []
    reference = tmp_path / "reference.txt"
    reference.write_text("reference", encoding="utf-8")

    class SlowClient:
        def is_available(self):
            return True

        async def _call_ai(self, *args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                events.append("request cancelled")

        async def close(self):
            events.append("closed")

    monkeypatch.setattr(prompt_utils, "AIClient", SlowClient)
    monkeypatch.setattr(prompt_utils, "CRITERIA_GENERATION_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(RuntimeError, match="已停止等待"):
        asyncio.run(prompt_utils.generate_criteria("requirements", str(reference)))
    assert events == ["request cancelled", "closed"]
