"""按前缀让 LLM 忽略消息（消息仍记录、其它插件仍可见，只是不触发回复）。

与 NA 内置配置项的区别
----------------------
- ``AI_IGNORED_PREFIXES`` / ``AI_CHAT_IGNORE_REGEX`` 是**入口级**过滤：
  命中后消息既不落库、也不进插件、也不触发 LLM，等于整条消息消失。
- 本插件用 ``MsgSignal.BLOCK_TRIGGER``：消息照常落库、其它插件的
  ``mount_on_user_message`` 照常收到，**只是不唤起 LLM 回复**。

已知边界
--------
``BLOCK_TRIGGER`` 只拦截"本次触发"。命中前缀的消息**仍会出现在后续对话的
Recent Messages 历史里**（被别的消息触发时 LLM 依然能读到），因为 NA 没有
暴露历史渲染钩子。若需要"对 LLM 完全不可见但插件仍可见"，必须改核心
（在 ``message_service`` 落库后跳过触发 + 在 ``templates/history.py`` 过滤）。
"""

from __future__ import annotations

import re
from typing import List, Optional, Pattern

from pydantic import Field

from nekro_agent.api import i18n
from nekro_agent.api.message import ChatMessage
from nekro_agent.api.plugin import ConfigBase, ExtraField, NekroPlugin
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.api.signal import MsgSignal

plugin = NekroPlugin(
    name="按前缀忽略消息",
    module_name="llm_ignore_prefix",
    description="命中指定前缀/正则的消息仍会记录到历史并交给其它插件处理，但不会触发 LLM 回复",
    version="1.0.0",
    author="NTidal",
    url="https://github.com/NTidal/nekro_llm_ignore_prefix",
    i18n_name=i18n.i18n_text(
        zh_CN="按前缀忽略消息",
        en_US="Ignore Messages by Prefix",
    ),
    i18n_description=i18n.i18n_text(
        zh_CN="命中指定前缀/正则的消息仍会记录到历史并交给其它插件处理，但不会触发 LLM 回复",
        en_US="Messages matching the configured prefixes/regex are still recorded and visible to other plugins, but will not trigger an LLM reply",
    ),
    allow_sleep=False,  # 纯钩子插件，必须常驻
)


@plugin.mount_config()
class IgnorePrefixConfig(ConfigBase):
    """按前缀忽略消息配置。"""

    ENABLED: bool = Field(
        default=True,
        title="启用插件",
        description="总开关，关闭后所有规则都不生效",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="启用插件", en_US="Enable Plugin"),
            i18n_description=i18n.i18n_text(
                zh_CN="总开关，关闭后所有规则都不生效",
                en_US="Master switch; when off, no rule applies",
            ),
        ).model_dump(),
    )
    PREFIXES: List[str] = Field(
        default=[],
        title="忽略的消息前缀",
        description="消息以其中任意一项开头时，只记录不触发 LLM 回复（区分大小写；留空则不拦截）",
        json_schema_extra=ExtraField(
            sub_item_name="前缀",
            i18n_title=i18n.i18n_text(
                zh_CN="忽略的消息前缀",
                en_US="Ignored Message Prefixes",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="消息以其中任意一项开头时，只记录不触发 LLM 回复（区分大小写；留空则不拦截）",
                en_US="Messages starting with any of these are recorded but will not trigger an LLM reply (case-sensitive; empty = disabled)",
            ),
        ).model_dump(),
    )
    REGEX_PATTERNS: List[str] = Field(
        default=[],
        title="忽略的消息正则",
        description="消息匹配其中任意一条正则时，只记录不触发 LLM 回复（search 语义，可用于后缀/包含匹配）",
        json_schema_extra=ExtraField(
            sub_item_name="表达式",
            i18n_title=i18n.i18n_text(
                zh_CN="忽略的消息正则",
                en_US="Ignored Message Regex Patterns",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="消息匹配其中任意一条正则时，只记录不触发 LLM 回复（search 语义，可用于后缀/包含匹配）",
                en_US="Messages matching any of these regexes are recorded but will not trigger an LLM reply (search semantics)",
            ),
        ).model_dump(),
    )
    TRIM_LEADING_WHITESPACE: bool = Field(
        default=True,
        title="匹配前去除前导空白",
        description="启用后「  #提示」这类带前导空格的消息也能命中前缀",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="匹配前去除前导空白",
                en_US="Trim Leading Whitespace Before Matching",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="启用后「  #提示」这类带前导空格的消息也能命中前缀",
                en_US="When enabled, messages with leading whitespace such as '  #note' still match prefixes",
            ),
        ).model_dump(),
    )
    PREFIX_WINS_OVER_AT: bool = Field(
        default=True,
        title="前缀优先于 @机器人",
        description="启用时，即使消息 @ 了机器人，命中前缀也仍然不触发；关闭则该消息按正常触发流程处理",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="前缀优先于 @机器人",
                en_US="Prefix Wins Over @Mention",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="启用时，即使消息 @ 了机器人，命中前缀也仍然不触发；关闭则该消息按正常触发流程处理",
                en_US="When enabled, a matching prefix still suppresses the reply even if the bot was mentioned; when disabled, such messages trigger normally",
            ),
        ).model_dump(),
    )
    INCLUDE_CHAT_KEYS: List[str] = Field(
        default=[],
        title="仅在指定频道生效",
        description="留空表示所有频道生效；填写后只有列出的 chat_key 会应用本规则",
        json_schema_extra=ExtraField(
            sub_item_name="chat_key",
            i18n_title=i18n.i18n_text(
                zh_CN="仅在指定频道生效",
                en_US="Only Apply in These Channels",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="留空表示所有频道生效；填写后只有列出的 chat_key 会应用本规则",
                en_US="Empty means all channels; otherwise only the listed chat_keys apply",
            ),
        ).model_dump(),
    )
    EXCLUDE_CHAT_KEYS: List[str] = Field(
        default=[],
        title="排除指定频道",
        description="列出的 chat_key 不应用本规则（优先级高于「仅在指定频道生效」）",
        json_schema_extra=ExtraField(
            sub_item_name="chat_key",
            i18n_title=i18n.i18n_text(
                zh_CN="排除指定频道",
                en_US="Exclude These Channels",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="列出的 chat_key 不应用本规则（优先级高于「仅在指定频道生效」）",
                en_US="Listed chat_keys are exempt (takes precedence over the include list)",
            ),
        ).model_dump(),
    )
    LOG_HITS: bool = Field(
        default=True,
        title="记录命中日志",
        description="命中时写入插件日志，便于确认规则是否生效",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="记录命中日志", en_US="Log Matches"),
            i18n_description=i18n.i18n_text(
                zh_CN="命中时写入插件日志，便于确认规则是否生效",
                en_US="Write a plugin log line on each match, useful to verify the rule works",
            ),
        ).model_dump(),
    )


config: IgnorePrefixConfig = plugin.get_config(IgnorePrefixConfig)

# 正则编译缓存：pattern -> compiled（编译失败的记为 None，避免每条消息重复抛异常）
_REGEX_CACHE: dict[str, Optional[Pattern[str]]] = {}


def _get_regex(pattern_text: str) -> Optional[Pattern[str]]:
    if pattern_text not in _REGEX_CACHE:
        try:
            _REGEX_CACHE[pattern_text] = re.compile(pattern_text)
        except re.error as e:
            plugin.logger.error(f"忽略正则编译失败，已跳过: {pattern_text!r} -> {e}")
            _REGEX_CACHE[pattern_text] = None
    return _REGEX_CACHE[pattern_text]


def _channel_applies(chat_key: str) -> bool:
    if chat_key in config.EXCLUDE_CHAT_KEYS:
        return False
    if config.INCLUDE_CHAT_KEYS:
        return chat_key in config.INCLUDE_CHAT_KEYS
    return True


def _match_rule(text: str) -> Optional[str]:
    """返回命中的规则描述，未命中返回 None。"""
    for prefix in config.PREFIXES:
        if prefix and text.startswith(prefix):
            return f"前缀 {prefix!r}"
    for pattern_text in config.REGEX_PATTERNS:
        if not pattern_text:
            continue
        regex = _get_regex(pattern_text)
        if regex and regex.search(text):
            return f"正则 {pattern_text!r}"
    return None


@plugin.mount_on_user_message()
async def on_user_message(_ctx: AgentCtx, message: ChatMessage) -> Optional[MsgSignal]:
    """命中规则时返回 BLOCK_TRIGGER：消息保留记录，仅阻止 LLM 触发。"""
    if not config.ENABLED:
        return None
    if not config.PREFIXES and not config.REGEX_PATTERNS:
        return None

    raw_text = message.content_text or ""
    text = raw_text.lstrip() if config.TRIM_LEADING_WHITESPACE else raw_text
    if not text:
        return None

    if not _channel_applies(message.chat_key):
        return None

    if message.is_tome and not config.PREFIX_WINS_OVER_AT:
        return None

    rule = _match_rule(text)
    if not rule:
        return None

    if config.LOG_HITS:
        plugin.logger.info(
            f"命中忽略规则（只记录不触发 LLM）: {rule} | chat={message.chat_key} "
            f"| sender={message.sender_nickname} | text={text[:48]!r}",
        )
    return MsgSignal.BLOCK_TRIGGER
