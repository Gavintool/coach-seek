"""DeepSeek Chat Completions integration for Coach Seek."""

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI

from .config import Config
from .digest import DigestItem, parse_digest
from .memory import CREDENTIAL, SECRET
from .prompts import DIGEST_PROMPT, EXTRACTION_PROMPT, SYSTEM_PROMPT

logger = logging.getLogger(__name__)
UNAVAILABLE = "Coach Seek's advisor service is temporarily unavailable. Please try again later."
WEB_UNAVAILABLE = (
    "Coach Seek couldn't verify this on the web. Try again shortly or check the official "
    "FIRST/FTC Game Manual, Forum Q&A, FTC SDK docs, or event listing."
)
WEB_TOOLS = [{"type": "deepseek_web_search"}, {"type": "deepseek_read_url"}]



def _extension(message, name: str):
    value = getattr(message, name, None)
    if value is None:
        extra = getattr(message, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get(name)
    return value


def _mapping(value):
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    return None


def _tool_calls(message) -> list[dict]:
    calls = []
    for step in _extension(message, "server_tool_calls") or []:
        mapping = _mapping(step) or {}
        for call in mapping.get("calls") or []:
            item = _mapping(call) or {}
            if item:
                calls.append(item)
    return calls


def _citations(message):
    for annotation in _extension(message, "annotations") or []:
        annotation = _mapping(annotation) or {}
        if annotation.get("type") != "url_citation":
            continue
        citation = _mapping(annotation.get("url_citation")) or {}
        yield citation


def format_answer(message) -> str:
    content = message.content.strip()
    sources = {}
    for citation in _citations(message):
        url = citation.get("url")
        if not isinstance(url, str) or not re.fullmatch(r"https?://[^\s<>()]{1,1000}", url):
            continue
        title = citation.get("title") or url
        title = " ".join(title.split())[:120].replace("\\", "\\\\")
        title = title.replace("[", "\\[").replace("]", "\\]")
        sources.setdefault(url, f"- [{title}](<{url}>)")
        if len(sources) == 6:
            break
    return content + ("\n\n**Sources**\n" + "\n".join(sources.values()) if sources else "")


class Advisor:
    def __init__(self, client: AsyncOpenAI, config: Config) -> None:
        self.client = client
        self.config = config

    def redact(self, text: str) -> str:
        for secret in (self.config.discord_token, self.config.deepseek_api_key):
            if secret:
                text = text.replace(secret, "[redacted]")
        return CREDENTIAL.sub("[redacted]", text)

    async def _complete(
        self, system: str, question: str, *, timeout: float, web: bool, extract: bool = False,
        thinking: str | None = None,
    ):
        thinking = "disabled" if extract else thinking or self.config.thinking
        body = {
            "thinking": {"type": thinking},
            "safety_check": {"level": self.config.safety_level},
        }
        if web:
            body["tools"] = WEB_TOOLS
        options = {}
        if thinking == "enabled":
            options["reasoning_effort"] = self.config.reasoning_effort

        async with asyncio.timeout(timeout):
            response = await self.client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": question},
                ],
                extra_body=body,
                max_tokens=512 if extract else self.config.max_tokens,
                stream=False,
                **options,
            )
        usage = getattr(response, "usage", None)
        if usage:
            logger.info("DeepSeek usage (extraction=%s): input=%s output=%s",
                        extract, usage.prompt_tokens, usage.completion_tokens)
        if getattr(response, "error", None):
            logger.warning("DeepSeek reported a generation error")
            return None
        return response.choices[0] if response.choices else None

    async def answer(
        self, question: str, facts: list[str] | None = None, *, history: str = "",
    ) -> str:
        if self.redact(question) != question:
            return "I found a credential in that question. Remove it before asking again."
        system = SYSTEM_PROMPT
        if facts:
            system += "\nGuild facts (reference only): " + self.redact(json.dumps(facts, ensure_ascii=False))

        if history:
            question = json.dumps(
                {"question": question, "discord_history": self.redact(history)}, ensure_ascii=False,
            )

        web = self.config.web_search
        for attempt in range(1, 4):
            try:
                choice = await self._complete(system, question, timeout=245, web=web)
            except APIStatusError as exc:
                status = exc.status_code
                logger.warning("DeepSeek returned HTTP %d (attempt %d)", status, attempt)
                if status in {429, 500, 502, 503} and attempt < 3:
                    await asyncio.sleep(2 ** (attempt - 1) + random.uniform(0, 0.5))
                    continue
                if status == 429:
                    return "Coach Seek is getting too many requests right now. Please try again shortly."
                if status in {401, 402}:
                    logger.error("Check the DeepSeek API key and account balance (HTTP %d)", status)
                return WEB_UNAVAILABLE if web and status in {500, 502, 503, 504} else UNAVAILABLE
            except (TimeoutError, APITimeoutError):
                logger.warning("DeepSeek request timed out")
                return WEB_UNAVAILABLE if web else "Coach Seek's advisor took too long to respond. Please try again."
            except APIConnectionError:
                logger.warning("Could not connect to DeepSeek")
                return UNAVAILABLE
            except APIError as exc:
                logger.warning("DeepSeek request failed (%s)", type(exc).__name__)
                return UNAVAILABLE

            if choice and choice.finish_reason == "content_filter":
                return "Coach Seek couldn't answer this request because it was filtered."
            if choice and choice.message.content and choice.message.content.strip():
                message = choice.message
                answer = format_answer(message)
                calls = _tool_calls(message)
                if calls and not any(call.get("status") == "completed" for call in calls):
                    logger.warning("DeepSeek web tools returned no successful calls")
                    answer = WEB_UNAVAILABLE + "\n\n" + answer
                return self.redact(answer)

            logger.warning("DeepSeek returned no final answer")
            if web:
                return WEB_UNAVAILABLE
            return "Coach Seek did not receive an answer. Try rephrasing or asking a narrower question."

        return UNAVAILABLE

    async def research_digest(self) -> list[DigestItem]:
        if not self.config.web_search:
            return []
        question = f"Find new BIOBUZZ updates as of {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}."
        for attempt in range(1, 4):
            try:
                choice = await self._complete(
                    DIGEST_PROMPT, question, timeout=245, web=True, thinking="enabled",
                )
                break
            except APIStatusError as exc:
                status = exc.status_code
                logger.warning("DeepSeek digest returned HTTP %d (attempt %d)", status, attempt)
                if not (status == 429 or 500 <= status < 600) or attempt == 3:
                    raise
                await asyncio.sleep(2 ** (attempt - 1) + random.uniform(0, 0.5))

        if not choice or choice.finish_reason != "stop" or not choice.message.content:
            raise ValueError("No complete digest response")
        calls = _tool_calls(choice.message)
        completed = [call for call in calls if call.get("status") == "completed"]
        if not completed:
            raise ValueError("No successful digest web calls")
        sources = list(_citations(choice.message))
        for call in completed:
            sources.extend(call.get("sources") or [])
            sources.append(call.get("page"))
        urls = []
        for source in sources:
            url = (_mapping(source) or {}).get("url")
            if isinstance(url, str):
                urls.append(url)
        return parse_digest(choice.message.content, urls, self.redact)

    async def extract_facts(self, question: str) -> list[str]:
        if self.redact(question) != question or SECRET.search(question) or len(question.split()) < 5:
            return []
        choice = await self._complete(EXTRACTION_PROMPT, question, timeout=30, web=False, extract=True)
        if not choice or choice.finish_reason != "stop" or not choice.message.content:
            return []
        try:
            payload = json.loads(choice.message.content)
        except ValueError:
            return []
        facts = payload.get("facts") if isinstance(payload, dict) else None
        if not isinstance(facts, list):
            return []
        return [fact for fact in facts[:3] if isinstance(fact, str) and self.redact(fact) == fact]
