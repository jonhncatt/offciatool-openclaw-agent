from __future__ import annotations

import re
from typing import Any


def message_has_explicit_local_path(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    return bool(re.search(r"(?:^|[\s(])(?:/[^\s]+|[A-Za-z][:\\：][\\/][^\s]*)", raw))


def has_file_like_lookup_token(text: str) -> bool:
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    tokens = re.findall(r"[a-z0-9][a-z0-9._-]{4,}", raw)
    tld_like_suffixes = (".com", ".cn", ".net", ".org", ".jp", ".io", ".dev")
    code_exts = {
        "c", "cc", "cpp", "cxx", "h", "hpp", "hh", "py", "js", "jsx", "ts", "tsx",
        "java", "go", "rs", "swift", "kt", "rb", "php", "sh", "ps1", "yaml", "yml",
        "json", "xml", "toml", "ini", "cfg", "md", "txt",
    }
    for token in tokens:
        if token.startswith(("http://", "https://", "www.")):
            continue
        if token.endswith(tld_like_suffixes):
            continue
        if "_" in token and len(token) >= 6:
            return True
        if "." in token:
            stem, _, suffix = token.rpartition(".")
            if stem and suffix in code_exts:
                return True
    return False


def should_auto_search_default_roots(
    agent: Any,
    user_message: str,
    attachment_metas: list[dict[str, Any]],
    *,
    news_hints: tuple[str, ...],
) -> bool:
    if attachment_metas:
        return False
    if agent._looks_like_inline_document_payload(user_message):
        return False
    text = str(user_message or "").strip().lower()
    if not text:
        return False
    if message_has_explicit_local_path(user_message):
        return False
    if "http://" in text or "https://" in text or any(hint in text for hint in news_hints):
        return False

    search_verbs = (
        "找", "查", "搜", "搜索", "查找", "定位", "look for", "find", "search", "locate",
    )
    local_targets = (
        "函数", "方法", "代码", "源码", "测试", "用例", "文件", "目录", "文件夹", "项目", "仓库",
        "repo", "master", "source", "src", "test", "tests", "case", "实现", "定义", "声明",
        "调用点", "module", "function", "method", "file", "directory", "folder", "implementation",
    )
    has_lookup_verb = any(verb in text for verb in search_verbs)
    has_local_target = any(target in text for target in local_targets)
    return has_lookup_verb and (has_local_target or agent._has_file_like_lookup_token(text))


def looks_like_local_code_lookup_request(
    agent: Any,
    user_message: str,
    attachment_metas: list[dict[str, Any]],
    *,
    news_hints: tuple[str, ...],
) -> bool:
    if attachment_metas:
        return False
    if agent._looks_like_inline_document_payload(user_message):
        return False
    text = str(user_message or "").strip().lower()
    if not text:
        return False
    if "http://" in text or "https://" in text or any(hint in text for hint in news_hints):
        return False

    local_scope_hints = (
        "路径", "目录", "文件夹", "目录下", "文件夹下", "路径下", "项目", "仓库", "repo", "workbench",
        "workspace", "master", "source", "src", "test", "tests", "folder", "directory", "project",
    )
    code_target_hints = (
        "函数", "方法", "实现", "定义", "声明", "调用点", "测试", "用例", "测试文件", "文件名",
        "module", "function", "method", "test", "case", "filename", "file name", "implementation",
        "call site", "definition",
    )
    lookup_hints = (
        "找", "查", "搜", "搜索", "查找", "定位", "解释", "分析", "说明", "梳理", "看看", "看下",
        "看一下", "look for", "find", "search", "locate", "explain", "analyze",
    )
    has_local_scope = message_has_explicit_local_path(user_message) or any(hint in text for hint in local_scope_hints)
    has_code_target = any(hint in text for hint in code_target_hints)
    has_lookup_intent = any(hint in text for hint in lookup_hints)
    has_file_like_token = agent._has_file_like_lookup_token(text)
    return (
        has_lookup_intent
        and (has_code_target or has_file_like_token)
        and (has_local_scope or agent._should_auto_search_default_roots(user_message, attachment_metas))
    )


def looks_like_code_generation_request(user_message: str, attachment_metas: list[dict[str, Any]]) -> bool:
    text = str(user_message or "").strip().lower()
    if not text:
        return False

    generation_hints = (
        "生成", "创建", "新建", "改", "修", "写", "编写", "实现", "开发", "补全", "重构", "改写",
        "修改", "修复", "替换", "更新", "写入", "保存", "generate", "create", "write", "implement",
        "build", "scaffold", "refactor", "rewrite", "modify", "fix", "replace", "update", "edit",
    )
    code_target_hints = (
        "代码", "函数", "类", "组件", "页面", "接口", "脚本", "测试", "单元测试", "模块", "变量",
        "参数", "字段", "头文件", "header", ".h", ".hpp", "plugin", "component", "page", "api",
        "endpoint", "script", "test", "class", "function", "module", ".py", ".ts", ".tsx", ".js",
        ".jsx", ".java", ".go", ".rs", ".cpp", ".c",
    )
    lookup_only_hints = (
        "找", "查", "搜", "搜索", "查找", "定位", "explain", "解释", "look for", "find", "search", "locate",
    )

    has_generation_intent = any(hint in text for hint in generation_hints)
    if not has_generation_intent:
        return False
    has_code_target = any(hint in text for hint in code_target_hints)
    if not has_code_target and not attachment_metas:
        return False
    if any(hint in text for hint in lookup_only_hints) and not has_generation_intent:
        return False
    return True


def looks_like_permission_gate_text(
    text: str,
    *,
    has_attachments: bool = False,
    request_requires_tools: bool = False,
) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    if len(lowered) > 5000:
        lowered = lowered[:5000]
    attachment_deferral_patterns = (
        "已完成解析", "已经完成解析", "已经完成了解析", "已解析完成", "已经解析完成", "无需调用工具",
        "无需再调用工具", "无需再次调用工具", "不需要调用工具", "不必调用工具", "already parsed",
        "already finished parsing", "no need to call tool", "no need to use tool", "no tools needed",
    )
    if has_attachments and any(p in lowered for p in attachment_deferral_patterns):
        return True

    general_gate_patterns = (
        "要不要", "是否继续", "是否要我", "是否直接搜索", "是否直接查", "直接搜索", "直接查", "先直接搜索",
        "先直接查", "能直接搜索吗", "可以直接搜索吗", "要不要直接搜索", "要不要我直接搜索", "你选", "请选择",
        "选一个", "选一种", "二选一", "do you want me to continue", "should i continue", "if you agree",
        "if agreed", "如你同意", "如果同意", "若你同意", "如果你同意", "如您同意", "若您同意", "同意的话",
        "同意继续", "回复同意继续", '回复“同意继续”', "回复'同意继续'", "授权继续", "是否可直接访问",
        "是否可以直接访问", "是否能直接访问", "可直接访问的目录", "可访问的目录", "是不是可访问",
        "请确认我可以读取", "请确认我能读取", "请确认可以读取", "请确认可读取", "请确认我可以访问",
        "请确认我可以查看", "请确认可访问", "请确认可以访问", "可否读取", "能否读取", "读取下面两个路径",
        "读取以下两个路径", "读取下列路径", "预览内容不完整", "预览不完整", "内容不完整（截断", "内容不完整(截断",
        "preview is incomplete", "preview was truncated", "content preview is truncated", "please confirm i can read",
        "can i read the following", "need to read the full file", "need to read the full document",
        "is workbench directly accessible", "is it directly accessible", "请提供完整文件名", "请给出完整文件名",
        "请提供完整的文件名", "请提供扩展名", "请给出扩展名", "需要扩展名", "需要文件扩展名", "需要完整文件名",
        "带扩展名", "完整文件名", "完整的文件名", "file extension", "with extension", "full filename",
        "exact filename", "请粘贴原文", "请贴原文", "请把原文贴", "请提供原文", "请先提供原文", "请先提供原文片段",
        "请先贴原文", "请先把原文贴", "请把代码贴出来", "请贴出完整代码", "请贴出原始代码", "paste the original",
        "paste the full code", "provide the original text",
    )
    if not request_requires_tools and not has_attachments:
        return any(p in lowered for p in general_gate_patterns)

    patterns = (
        *general_gate_patterns,
        "两种方案", "可行方案", "方案a", "方案b", "工具未启用", "还没有被激活", "工具接口", "无法触发",
        "系统不执行写入", "绝对路径", "具体路径", "完整路径", "文件夹路径", "请告诉我", "你可以告诉我", "继续读取吗",
        "继续读吗", "继续读取其他部分", "继续查看其他部分", "需要继续读取", "需要继续读", "需要读取其他部分",
        "需要读其他部分", "怕太大", "太大", "文件太大", "内容太大", "最终确认", "确认句", "无需你回答",
        "不执行写入", "触发工具调用", "必须包含路径", "需要你同意", "需要你的同意", "需要你回复同意继续",
        "need your confirmation", "do you want me to continue", "should i continue", "please provide instructions",
        "你当前的指示中没有新增对读取附件内容的要求", "没有新增对读取附件内容的要求", "若后续需要解析",
        "后续需要解析", "无需调用工具", "无需再调用工具", "无需再次调用工具", "不需要调用工具", "已完成解析",
        "已经完成了解析", "已解析完成", "write_text_file", "append_text_file", "directly search", "search directly",
        "absolute path", "full path", "full filename", "exact filename", "file extension", "with extension",
    )
    if re.search(r"请确认.{0,24}(?:读取|访问|查看).{0,24}(?:路径|文件|附件)", lowered):
        return True
    if re.search(r"confirm.{0,30}(?:read|access|open).{0,30}(?:path|file|attachment)", lowered):
        return True
    if not any(p in lowered for p in patterns):
        return False
    file_hints = (
        "文件", "读取", "写入", "生成", "保存", "read_text_file", "write_text_file", "append_text_file", "chunk",
        "附件", "邮件", "文档", "path", "扩展名", "文件名", "解析", "搜索", "函数", "目录", "文件夹",
    )
    return any(h in lowered for h in file_hints)
