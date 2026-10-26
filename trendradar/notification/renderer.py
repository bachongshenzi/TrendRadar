# coding=utf-8
"""
通知内容渲染模块

提供多平台通知内容渲染功能，生成格式化的推送消息
"""

import re
import difflib
from datetime import datetime
from typing import Dict, List, Optional, Callable

from trendradar.report.formatter import format_title_for_platform


# 默认区域顺序
DEFAULT_REGION_ORDER = ["hotlist", "rss", "new_items", "standalone", "ai_analysis"]


# ==================== 跨平台同题合并 ====================

# 标题归一化：只保留 数字/小写字母/中文/日文假名，标点、空白、emoji 全部剔除
_DEDUP_KEEP_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff]+")
# 常见电讯前缀：「10月24日电，」「据报」「据悉」「报道：」等，去掉后提升同题命中率
_DEDUP_PREFIX_RE = re.compile(
    r"^(\d{1,2}月\d{1,2}日[电讯]?|据报|据悉|据消息|报道|媒体|外媒|快讯|消息|独家|重磅|官方)[，,:\s：]*"
)

# 公共前缀长度达到该阈值即判定为同题（跨平台改写标题时头部实体通常一致）
_DEDUP_PREFIX_HIT = 9
# 整体相似度阈值（长度接近时启用，兜住中间改写的情况）
_DEDUP_RATIO_HIT = 0.85


def _normalize_title_for_dedup(title: str) -> str:
    """标题归一化：用于跨平台同题判重

    规则：转小写 → 去常见电讯前缀 → 只保留 数字/字母/中文/假名（标点空白全清）

    Args:
        title: 原始标题

    Returns:
        归一化后的判重键
    """
    if not isinstance(title, str):
        title = str(title)
    t = title.lower()
    t = _DEDUP_PREFIX_RE.sub("", t)
    t = _DEDUP_KEEP_RE.sub("", t)
    return t


def _is_same_story(key1: str, key2: str) -> bool:
    """判断两个归一化标题是否为同一件事

    三条判据（依次放宽）：
    1. 归一化后完全相同
    2. 公共前缀 >= _DEDUP_PREFIX_HIT（跨平台转载时主语/实体通常写在最前）
    3. 长度接近时整体相似度 >= _DEDUP_RATIO_HIT

    Args:
        key1: 已归一化的标题键
        key2: 已归一化的标题键

    Returns:
        True 表示判定为同题
    """
    if not key1 or not key2:
        return False
    if key1 == key2:
        return True

    short, long_ = (key1, key2) if len(key1) <= len(key2) else (key2, key1)
    common = 0
    for ch1, ch2 in zip(short, long_):
        if ch1 != ch2:
            break
        common += 1
    if common >= _DEDUP_PREFIX_HIT:
        return True

    # 长度差超过 50% 视为不同粒度（简讯 vs 深度稿），不做相似度合并
    if abs(len(key1) - len(key2)) <= len(long_) * 0.5:
        if difflib.SequenceMatcher(None, key1, key2).ratio() >= _DEDUP_RATIO_HIT:
            return True

    return False


def merge_duplicate_titles(titles: List[Dict]) -> List[Dict]:
    """跨平台同题合并：相同标题只保留一条，其余来源挂到 extra_sources 字段

    仅在推送渲染阶段调用，不影响数据库存储、AI 分析和 HTML 报告。
    合并发生在每个关键词组内部；同一条新闻命中多个关键词组时，各组仍会各自展示。

    Args:
        titles: 同一关键词组内的标题数据列表

    Returns:
        合并后的列表；被合并的主条目字典带 extra_sources（其他来源名列表），
        无重复的条目不含该字段
    """
    if not titles:
        return titles

    merged: List[Dict] = []
    keys: List[str] = []

    for title_data in titles:
        key = _normalize_title_for_dedup(title_data.get("title", ""))
        hit_index = None
        if key:
            for idx, existing_key in enumerate(keys):
                if _is_same_story(key, existing_key):
                    hit_index = idx
                    break

        if hit_index is not None:
            main = merged[hit_index]
            extra_sources = main.setdefault("extra_sources", [])
            source_name = title_data.get("source_name", "")
            if source_name and source_name not in extra_sources:
                extra_sources.append(source_name)
            # 累计出现次数，让 (N次) 反映跨平台报道热度
            main["count"] = main.get("count", 1) + title_data.get("count", 1)
        else:
            merged.append(dict(title_data))
            keys.append(key)

    return merged


def render_feishu_content(
    report_data: Dict,
    update_info: Optional[Dict] = None,
    mode: str = "daily",
    separator: str = "---",
    region_order: Optional[List[str]] = None,
    get_time_func: Optional[Callable[[], datetime]] = None,
    rss_items: Optional[list] = None,
    show_new_section: bool = True,
) -> str:
    """渲染飞书通知内容（支持热榜+RSS合并）

    Args:
        report_data: 报告数据字典，包含 stats, new_titles, failed_ids, total_new_count
        update_info: 版本更新信息（可选）
        mode: 报告模式 ("daily", "incremental", "current")
        separator: 内容分隔符
        region_order: 区域显示顺序列表
        get_time_func: 获取当前时间的函数（可选，默认使用 datetime.now()）
        rss_items: RSS 条目列表（可选，用于合并推送）
        show_new_section: 是否显示新增热点区域

    Returns:
        格式化的飞书消息内容
    """
    if region_order is None:
        region_order = DEFAULT_REGION_ORDER

    # 生成热点词汇统计部分
    stats_content = ""
    if report_data["stats"]:
        stats_content += "📊 **热点词汇统计**\n\n"

        total_count = len(report_data["stats"])

        for i, stat in enumerate(report_data["stats"]):
            word = stat["word"]
            count = stat["count"]

            sequence_display = f"<font color='grey'>[{i + 1}/{total_count}]</font>"

            if count >= 10:
                stats_content += f"🔥 {sequence_display} **{word}** : <font color='red'>{count}</font> 条\n\n"
            elif count >= 5:
                stats_content += f"📈 {sequence_display} **{word}** : <font color='orange'>{count}</font> 条\n\n"
            else:
                stats_content += f"📌 {sequence_display} **{word}** : {count} 条\n\n"

            # 跨平台同题合并：相同标题只展示一条，其余来源以副行标注
            merged_titles = merge_duplicate_titles(stat["titles"])
            for j, title_data in enumerate(merged_titles, 1):
                formatted_title = format_title_for_platform(
                    "feishu", title_data, show_source=True
                )
                stats_content += f"  {j}. {formatted_title}\n"

                if j < len(merged_titles):
                    stats_content += "\n"

            if i < len(report_data["stats"]) - 1:
                stats_content += f"\n{separator}\n\n"

    # 生成新增新闻部分
    new_titles_content = ""
    if show_new_section and report_data["new_titles"]:
        new_titles_content += (
            f"🆕 **本次新增热点新闻** (共 {report_data['total_new_count']} 条)\n\n"
        )

        for source_data in report_data["new_titles"]:
            new_titles_content += (
                f"**{source_data['source_name']}** ({len(source_data['titles'])} 条):\n"
            )

            for j, title_data in enumerate(source_data["titles"], 1):
                title_data_copy = title_data.copy()
                title_data_copy["is_new"] = False
                formatted_title = format_title_for_platform(
                    "feishu", title_data_copy, show_source=False
                )
                new_titles_content += f"  {j}. {formatted_title}\n"

            new_titles_content += "\n"

    # RSS 内容
    rss_content = ""
    if rss_items:
        rss_content = _render_rss_section_feishu(rss_items, separator)

    # 准备各区域内容映射
    region_contents = {
        "hotlist": stats_content,
        "new_items": new_titles_content,
        "rss": rss_content,
    }

    # 按 region_order 顺序组装内容
    text_content = ""
    for region in region_order:
        content = region_contents.get(region, "")
        if content:
            if text_content:
                text_content += f"\n{separator}\n\n"
            text_content += content

    if not text_content:
        if mode == "incremental":
            mode_text = "增量模式下暂无新增匹配的热点词汇"
        elif mode == "current":
            mode_text = "当前榜单模式下暂无匹配的热点词汇"
        else:
            mode_text = "暂无匹配的热点词汇"
        text_content = f"📭 {mode_text}\n\n"

    if report_data["failed_ids"]:
        if text_content and "暂无匹配" not in text_content:
            text_content += f"\n{separator}\n\n"

        text_content += "⚠️ **数据获取失败的平台：**\n\n"
        for i, id_value in enumerate(report_data["failed_ids"], 1):
            text_content += f"  • <font color='red'>{id_value}</font>\n"

    # 获取当前时间
    now = get_time_func() if get_time_func else datetime.now()
    text_content += (
        f"\n\n<font color='grey'>更新时间：{now.strftime('%Y-%m-%d %H:%M:%S')}</font>"
    )

    if update_info:
        text_content += f"\n<font color='grey'>TrendRadar 发现新版本 {update_info['remote_version']}，当前 {update_info['current_version']}</font>"

    return text_content


def render_dingtalk_content(
    report_data: Dict,
    update_info: Optional[Dict] = None,
    mode: str = "daily",
    region_order: Optional[List[str]] = None,
    get_time_func: Optional[Callable[[], datetime]] = None,
    rss_items: Optional[list] = None,
    show_new_section: bool = True,
) -> str:
    """渲染钉钉通知内容（支持热榜+RSS合并）

    Args:
        report_data: 报告数据字典，包含 stats, new_titles, failed_ids, total_new_count
        update_info: 版本更新信息（可选）
        mode: 报告模式 ("daily", "incremental", "current")
        region_order: 区域显示顺序列表
        get_time_func: 获取当前时间的函数（可选，默认使用 datetime.now()）
        rss_items: RSS 条目列表（可选，用于合并推送）
        show_new_section: 是否显示新增热点区域

    Returns:
        格式化的钉钉消息内容
    """
    if region_order is None:
        region_order = DEFAULT_REGION_ORDER

    total_titles = sum(
        len(stat["titles"]) for stat in report_data["stats"] if stat["count"] > 0
    )
    now = get_time_func() if get_time_func else datetime.now()

    # 头部信息由 splitter 统一构建，此处不再重复
    header_content = ""

    # 生成热点词汇统计部分
    stats_content = ""
    if report_data["stats"]:
        stats_content += "📊 **热点词汇统计**\n\n"

        total_count = len(report_data["stats"])

        for i, stat in enumerate(report_data["stats"]):
            word = stat["word"]
            count = stat["count"]

            sequence_display = f"[{i + 1}/{total_count}]"

            if count >= 10:
                stats_content += f"🔥 {sequence_display} **{word}** : **{count}** 条\n\n"
            elif count >= 5:
                stats_content += f"📈 {sequence_display} **{word}** : **{count}** 条\n\n"
            else:
                stats_content += f"📌 {sequence_display} **{word}** : {count} 条\n\n"

            for j, title_data in enumerate(stat["titles"], 1):
                formatted_title = format_title_for_platform(
                    "dingtalk", title_data, show_source=True
                )
                stats_content += f"  {j}. {formatted_title}\n"

                if j < len(stat["titles"]):
                    stats_content += "\n"

            if i < len(report_data["stats"]) - 1:
                stats_content += f"\n---\n\n"

    # 生成新增新闻部分
    new_titles_content = ""
    if show_new_section and report_data["new_titles"]:
        new_titles_content += (
            f"🆕 **本次新增热点新闻** (共 {report_data['total_new_count']} 条)\n\n"
        )

        for source_data in report_data["new_titles"]:
            new_titles_content += f"**{source_data['source_name']}** ({len(source_data['titles'])} 条):\n\n"

            for j, title_data in enumerate(source_data["titles"], 1):
                title_data_copy = title_data.copy()
                title_data_copy["is_new"] = False
                formatted_title = format_title_for_platform(
                    "dingtalk", title_data_copy, show_source=False
                )
                new_titles_content += f"  {j}. {formatted_title}\n"

            new_titles_content += "\n"

    # RSS 内容
    rss_content = ""
    if rss_items:
        rss_content = _render_rss_section_markdown(rss_items)

    # 准备各区域内容映射
    region_contents = {
        "hotlist": stats_content,
        "new_items": new_titles_content,
        "rss": rss_content,
    }

    # 按 region_order 顺序组装内容
    text_content = header_content
    has_content = False
    for region in region_order:
        content = region_contents.get(region, "")
        if content:
            if has_content:
                text_content += "\n---\n\n"
            text_content += content
            has_content = True

    if not has_content:
        if mode == "incremental":
            mode_text = "增量模式下暂无新增匹配的热点词汇"
        elif mode == "current":
            mode_text = "当前榜单模式下暂无匹配的热点词汇"
        else:
            mode_text = "暂无匹配的热点词汇"
        text_content += f"📭 {mode_text}\n\n"

    if report_data["failed_ids"]:
        if "暂无匹配" not in text_content:
            text_content += "\n---\n\n"

        text_content += "⚠️ **数据获取失败的平台：**\n\n"
        for i, id_value in enumerate(report_data["failed_ids"], 1):
            text_content += f"  • **{id_value}**\n"

    text_content += f"\n\n> 更新时间：{now.strftime('%Y-%m-%d %H:%M:%S')}"

    if update_info:
        text_content += f"\n> TrendRadar 发现新版本 **{update_info['remote_version']}**，当前 **{update_info['current_version']}**"

    return text_content


# === RSS 内容渲染辅助函数（用于合并推送） ===

def _render_rss_section_feishu(rss_items: list, separator: str = "---") -> str:
    """渲染 RSS 内容区块（飞书格式，用于合并推送）"""
    if not rss_items:
        return ""

    # 按 feed_id 分组
    feeds_map: Dict[str, list] = {}
    for item in rss_items:
        feed_id = item.get("feed_id", "unknown")
        if feed_id not in feeds_map:
            feeds_map[feed_id] = []
        feeds_map[feed_id].append(item)

    text_content = f"📰 **RSS 订阅更新** (共 {len(rss_items)} 条)\n\n"

    for feed_id, items in feeds_map.items():
        feed_name = items[0].get("feed_name", feed_id) if items else feed_id

        text_content += f"**{feed_name}** ({len(items)} 条)\n\n"

        for i, item in enumerate(items, 1):
            title = item.get("title", "")
            url = item.get("url", "")
            published_at = item.get("published_at", "")

            if url:
                text_content += f"  {i}. [{title}]({url})"
            else:
                text_content += f"  {i}. {title}"

            if published_at:
                text_content += f" <font color='grey'>- {published_at}</font>"

            text_content += "\n"

        text_content += "\n"

    return text_content.rstrip("\n")


def _render_rss_section_markdown(rss_items: list) -> str:
    """渲染 RSS 内容区块（通用 Markdown 格式，用于合并推送）"""
    if not rss_items:
        return ""

    # 按 feed_id 分组
    feeds_map: Dict[str, list] = {}
    for item in rss_items:
        feed_id = item.get("feed_id", "unknown")
        if feed_id not in feeds_map:
            feeds_map[feed_id] = []
        feeds_map[feed_id].append(item)

    text_content = f"📰 **RSS 订阅更新** (共 {len(rss_items)} 条)\n\n"

    for feed_id, items in feeds_map.items():
        feed_name = items[0].get("feed_name", feed_id) if items else feed_id

        text_content += f"**{feed_name}** ({len(items)} 条)\n"

        for i, item in enumerate(items, 1):
            title = item.get("title", "")
            url = item.get("url", "")
            published_at = item.get("published_at", "")

            if url:
                text_content += f"  {i}. [{title}]({url})"
            else:
                text_content += f"  {i}. {title}"

            if published_at:
                text_content += f" `{published_at}`"

            text_content += "\n"

        text_content += "\n"

    return text_content.rstrip("\n")
