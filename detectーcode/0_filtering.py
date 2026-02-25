# 0_filtering.py

# 1）../output から before/after ペアを探索し、
# 2) PATTERNS を含むペアだけに絞り込み
# 3) Tree-sitter + GumTree で関数定義/呼び出し差分を抽出
# 4) REST API らしき変更だけを OUTPUT_FILE に保存

import os
import json
import threading
import itertools
import time
import re
import tempfile
import subprocess
import shutil
from typing import Optional, Tuple, List, Dict, Any
from tree_sitter import Language, Parser
from dotenv import load_dotenv

load_dotenv()

OUTPUT_FILE = "0_filtered.json"

PATTERNS = [r"switch-bot\.com", r"switch-bot", r"switchbot", r"switch bot", r"switch_bot"] 
# PATTERNS = [r"api\.fitbit\.com", r"fitbit"]
# PATTERNS = [r"clip\/v2\/", r"hue", r"philipshue", r"philips_hue"]

GUMTREE_BIN = os.environ.get("GUMTREE_BIN") or shutil.which("gumtree")
if not GUMTREE_BIN:
    raise RuntimeError(
        "GUMTREE_BIN が設定されておらず、gumtree が PATH から見つかりません。"
    )
GUMTREE_BIN = os.path.abspath(GUMTREE_BIN)
TREE_SITTER_LANGUAGES = "build/my-languages.so"
LANGUAGE_MAP = {
    ".py": "python",
    ".java": "java",
    ".js": "javascript",
}
loaded_langs: Dict[str, Language] = {}

EXT_TO_LANG = {
    ".py": "python",
    ".java": "java",
    ".js": "javascript",
}

REST_HINTS_BY_LANG: Dict[str, List[str]] = {
    "python": [
        "requests.get",
        "requests.post",
        "requests.put",
        "requests.patch",
        "requests.delete",
        "httpx.get",
        "httpx.post",
        "httpx.put",
        "httpx.patch",
        "httpx.delete",
        "urllib.request.urlopen",
    ],
    "java": [
        "HttpRequest.newBuilder",
        "HttpClient.newBuilder",
        "Request.Builder",
        "OkHttpClient.Builder",
        "HttpGet",
        "HttpPost",
        "HttpPut",
        "HttpPatch",
        "HttpDelete",
    ],
    "javascript": [
        "fetch(",
        "axios.get",
        "axios.post",
        "axios.put",
        "axios.patch",
        "axios.delete",
        "XMLHttpRequest",
        "$.ajax",
        "$.get",
        "$.post",
        "$.put",
        "$.delete",
        "$.patch",
    ],
}

KEYWORDS_BY_LANG: Dict[str, List[str]] = {
    "python": ["request", "response"],
    "java": ["request", "response"],
    "javascript": ["response"],
}


def show_spinner(stop_event: threading.Event):
    spinner = itertools.cycle(["|", "/", "-", "\\"])
    while not stop_event.is_set():
        print(f"⏳ 処理中 {next(spinner)}", end="\r")
        time.sleep(0.2)


def matches_pattern(line: str) -> bool:
    return any(re.search(p, line, flags=re.IGNORECASE) for p in PATTERNS)

def has_pattern(code_before: str, code_after: str) -> bool:
    text = code_before + code_after
    return any(matches_pattern(line) for line in text.splitlines())


def detect_language(path: str) -> Language:
    """Tree-sitter 用: 拡張子から Language オブジェクトを返す"""
    ext = os.path.splitext(path)[1].lower()
    name = LANGUAGE_MAP.get(ext)
    if name is None:
        raise ValueError(f"未対応の拡張子です: {ext} (path={path})")
    if name not in loaded_langs:
        loaded_langs[name] = Language(TREE_SITTER_LANGUAGES, name)
    return loaded_langs[name]


def detect_lang_name_for_filter(path: str) -> Optional[str]:
    """REST フィルタ用: 'python' / 'java' / 'javascript' を返す"""
    ext = os.path.splitext(path)[1].lower()
    return EXT_TO_LANG.get(ext)


def find_all_patch_code_pairs_with_url(base_dir: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []

    for dirpath, dirnames, filenames in os.walk(base_dir):
        # 1 ディレクトリ 1 ペア前提
        before_file = next((f for f in filenames if f.startswith("before")), None)
        after_file = next((f for f in filenames if f.startswith("after")), None)
        commit_url_file = os.path.join(dirpath, "commit_url.txt")
        file_name_file = os.path.join(dirpath, "file_name.txt")

        if before_file and after_file:
            record: Dict[str, Any] = {
                "path_before": os.path.join(dirpath, before_file),
                "path_after": os.path.join(dirpath, after_file),
                "commit_url": None,
                "file_name": None,
            }

            if os.path.exists(commit_url_file):
                with open(commit_url_file, "r", encoding="utf-8") as f:
                    record["commit_url"] = f.read().strip()

            if os.path.exists(file_name_file):
                with open(file_name_file, "r", encoding="utf-8") as f:
                    record["file_name"] = f.read().strip()

            records.append(record)

    return records


def extract_primary_http_call_from_node(
    code: str,
    node,
    patterns: List[str],
) -> Tuple[Optional[str], Optional[int]]:
    """
    ノード内テキストから、patterns のうち最初に出現する HTTP 呼び出しを 1 つ抽出する。
    patterns は REST_HINTS_BY_LANG["python"] などから渡す。
    """
    text = code[node.start_byte: node.end_byte]

    lowest_idx = -1
    for pat in patterns:
        idx = text.find(pat)
        if idx != -1 and (lowest_idx == -1 or idx < lowest_idx):
            lowest_idx = idx

    if lowest_idx == -1:
        return None, None

    abs_byte_idx = node.start_byte + lowest_idx
    start_line = code.count("\n", 0, abs_byte_idx) + 1

    sub = text[lowest_idx:]
    paren_pos = sub.find("(")
    if paren_pos == -1:
        # 括弧がない場合は行末までを呼び出しとみなす
        line_end = sub.find("\n")
        if line_end == -1:
            call_text = sub.strip()
        else:
            call_text = sub[:line_end].strip()
        return call_text, start_line

    depth = 0
    seen_first_paren = False
    end_idx = len(sub)
    for i in range(paren_pos, len(sub)):
        ch = sub[i]
        if ch == "(":
            depth += 1
            seen_first_paren = True
        elif ch == ")":
            depth -= 1
            if depth == 0 and seen_first_paren:
                end_idx = i + 1
                break

    call_text = sub[:end_idx].strip()
    return call_text, start_line


def extract_calls_with_lines(code: str, lang: Language) -> List[Dict[str, Any]]:
    parser = Parser()
    parser.set_language(lang)
    tree = parser.parse(code.encode("utf-8"))

    def node_text(node):
        return code[node.start_byte: node.end_byte]

    calls: List[Dict[str, Any]] = []

    def add_call_generic(node, func_node, args_node):
        if not func_node:
            return
        fn_name = node_text(func_node).strip()
        if not fn_name:
            return
        args_text = node_text(args_node).strip() if args_node else ""
        calls.append(
            {
                "call": f"{fn_name}{args_text}",
                "line": node.start_point[0] + 1,
            }
        )

    def walk(node):
        # Python: REST_HINTS_BY_LANG["python"] をパターンとして利用
        if lang.name == "python" and node.type == "call":
            patterns = REST_HINTS_BY_LANG.get("python", [])
            call_text, line = extract_primary_http_call_from_node(
                code, node, patterns
            )
            if call_text:
                calls.append(
                    {
                        "call": call_text,
                        "line": line if line is not None else (node.start_point[0] + 1),
                    }
                )

        # JavaScript
        elif lang.name == "javascript" and node.type == "call_expression":
            func_node = node.child_by_field_name("function")
            args_node = node.child_by_field_name("arguments")
            add_call_generic(node, func_node, args_node)

        # Java
        elif lang.name == "java" and node.type == "method_invocation":
            func_node = node.child_by_field_name("name")
            args_node = node.child_by_field_name("arguments")
            add_call_generic(node, func_node, args_node)

        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return calls


def extract_function_definitions(code: str, lang: Language) -> Dict[str, Dict[str, Any]]:
    """
    Tree-sitter のフィールドを使って関数定義を抽出する。
    """
    parser = Parser()
    parser.set_language(lang)
    tree = parser.parse(code.encode("utf-8"))

    def node_text(node):
        return code[node.start_byte: node.end_byte]

    func_defs: Dict[str, Dict[str, Any]] = {}

    def add_func(name_node, code_node):
        if not name_node or not code_node:
            return
        name = node_text(name_node).strip()
        if not name:
            return
        func_defs[name] = {
            "code": node_text(code_node).strip(),
            "start_line": code_node.start_point[0] + 1,
            "end_line": code_node.end_point[0] + 1,
        }

    def walk(node):
        if lang.name == "python":
            if node.type in ("function_definition", "async_function_definition"):
                name_node = node.child_by_field_name("name")
                add_func(name_node, node)
            elif node.type == "decorated_definition":
                inner_def = node.child_by_field_name("definition")
                if inner_def and inner_def.type in (
                    "function_definition",
                    "async_function_definition",
                ):
                    name_node = inner_def.child_by_field_name("name")
                    add_func(name_node, node)

        elif lang.name == "java":
            if node.type == "method_declaration":
                name_node = node.child_by_field_name("name")
                add_func(name_node, node)

        elif lang.name == "javascript":
            if node.type == "function_declaration":
                name_node = node.child_by_field_name("name")
                add_func(name_node, node)
            elif node.type == "method_definition":
                name_node = node.child_by_field_name("name")
                add_func(name_node, node)

        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return func_defs


def get_gumtree_change_count(code1: str, code2: str, ext: str = ".txt") -> int:
    with tempfile.NamedTemporaryFile(
        delete=False, mode="w", encoding="utf-8", suffix=ext
    ) as f1, tempfile.NamedTemporaryFile(
        delete=False, mode="w", encoding="utf-8", suffix=ext
    ) as f2:
        f1.write(code1)
        f2.write(code2)
        f1_path, f2_path = f1.name, f2.name

    try:
        result = subprocess.run(
            [GUMTREE_BIN, "textdiff", f1_path, f2_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        output = result.stdout
        return output.count("update") + output.count("insert") + output.count("delete")
    finally:
        try:
            os.remove(f1_path)
        except OSError:
            pass
        try:
            os.remove(f2_path)
        except OSError:
            pass


# ===＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝

if __name__ == "__main__":
    # --- 1. ../output 配下の対象ディレクトリを選択 ---
    base = os.path.join("..", "output")
    level1_dirs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]

    print("🔍 対象ディレクトリを選択してください:")
    for i, d in enumerate(level1_dirs):
        print(f"{i}: {d}")

    idx = int(input("番号: "))
    selected_dir = os.path.join(base, level1_dirs[idx])

    # --- 2. before/after ペアを探索 ---
    all_pairs = find_all_patch_code_pairs_with_url(selected_dir)
    total_pairs = len(all_pairs)
    print(f"📦 対象ペア数（before/after）: {total_pairs}")

    results: List[Dict[str, Any]] = []

    stop_event = threading.Event()
    spinner_thread = threading.Thread(target=show_spinner, args=(stop_event,))
    spinner_thread.start()

    try:
        for pair in all_pairs:
            path_before = pair["path_before"]
            path_after = pair["path_after"]
            commit_url = pair.get("commit_url")
            file_name = pair.get("file_name")

            try:
                with open(path_before, encoding="utf-8") as f1, open(
                    path_after, encoding="utf-8"
                ) as f2:
                    code_before = f1.read()
                    code_after = f2.read()
            except Exception as e:
                print(f"\n⚠️ ファイル読み込みエラー ({path_before}, {path_after}): {e}")
                continue

            # --- 3. URL パターンを含まないペアはスキップ ---
            if not has_pattern(code_before, code_after):
                continue

            # Tree-sitter 言語判定
            try:
                lang = detect_language(path_before)
            except ValueError as e:
                print(f"\n⚠️ 言語判定エラー ({path_before}): {e}")
                continue

            ext = os.path.splitext(path_before)[1].lower()

            # --- 4. 関数呼び出し差分 ---
            calls_before = extract_calls_with_lines(code_before, lang)
            calls_after = extract_calls_with_lines(code_after, lang)

            call_texts_before = {c["call"] for c in calls_before}
            call_texts_after = {c["call"] for c in calls_after}

            removed_calls = [
                c for c in calls_before if c["call"] not in call_texts_after
            ]
            added_calls = [c for c in calls_after if c["call"] not in call_texts_before]

            # --- 5. 関数定義差分 ---
            defs_before = extract_function_definitions(code_before, lang)
            defs_after = extract_function_definitions(code_after, lang)

            changed_defs: List[str] = []
            def_bodies_after: Dict[str, Dict[str, Any]] = {}
            def_bodies_before: Dict[str, Dict[str, Any]] = {}

            # 「同名だが中身が変わった関数」と
            # 「新規 or 名前変更された関数」を区別するための集合
            same_name_changed: set = set()
            new_or_renamed: set = set()

            # 共通名の関数で差分あり
            for fname in defs_before.keys() & defs_after.keys():
                if defs_before[fname]["code"] != defs_after[fname]["code"]:
                    changed_defs.append(fname)
                    def_bodies_after[fname] = defs_after[fname]
                    def_bodies_before[fname] = defs_before[fname]
                    same_name_changed.add(fname)

            # 新規 or 名前変更された関数を GumTree でマッチング
            for new_fname in defs_after.keys() - defs_before.keys():
                new_body = defs_after[new_fname]["code"]
                matched = False
                for old_fname, old in defs_before.items():
                    change_count = get_gumtree_change_count(
                        old["code"], new_body, ext
                    )
                    # change_count <= 7 を「対応あり」とみなす
                    if change_count <= 7:
                        changed_defs.append(new_fname)
                        def_bodies_after[new_fname] = defs_after[new_fname]
                        def_bodies_before[new_fname] = old
                        new_or_renamed.add(new_fname)
                        matched = True
                        break
                if not matched:
                    changed_defs.append(new_fname)
                    def_bodies_after[new_fname] = defs_after[new_fname]
                    def_bodies_before[new_fname] = {
                        "code": "",
                        "start_line": None,
                        "end_line": None,
                    }
                    new_or_renamed.add(new_fname)

            # --- 6. REST API っぽい変更かどうかを reasons に詰める ---
            lang_name = detect_lang_name_for_filter(path_before)
            search_words = [w.lower() for w in KEYWORDS_BY_LANG.get(lang_name or "", [])]
            rest_hints = REST_HINTS_BY_LANG.get(lang_name or "", [])

            reasons: List[Dict[str, Any]] = []

            # ① 同名関数の定義差分 + キーワード一致
            for fname in same_name_changed:
                body = def_bodies_after.get(fname, {}).get("code", "")
                if search_words and all(w in body.lower() for w in search_words):
                    reasons.append(
                        {
                            "type": "same_name_function_changed",
                            "function": fname,
                            "matched_keywords": search_words,
                        }
                    )

            # ② 新規または名前変更された関数 + キーワード一致
            for fname in new_or_renamed:
                body = def_bodies_after.get(fname, {}).get("code", "")
                if search_words and all(w in body.lower() for w in search_words):
                    reasons.append(
                        {
                            "type": "new_or_renamed_function",
                            "function": fname,
                            "matched_keywords": search_words,
                        }
                    )

            # ③ 関数呼び出し文差分（REST APIらしきもの）
            for call_entry in added_calls:
                call_text_raw = call_entry.get("call") or ""
                call_text = call_text_raw.lower()
                hit_words = [hint for hint in rest_hints if hint.lower() in call_text]
                if hit_words:
                    reasons.append(
                        {
                            "type": "rest_api_call_added",
                            "call": call_text_raw,
                            "matched_keywords": hit_words,
                            "language": lang_name,
                        }
                    )

            # REST っぽいものが 1 つも無ければこのペアはスキップ
            if not reasons:
                continue

            # step5.py で必要な情報＋reasons を 1 レコードにまとめる
            results.append(
                {
                    "path_before": path_before,
                    "path_after": path_after,
                    "commit_url": commit_url,
                    "file_name": file_name,
                    "removed_calls": removed_calls,
                    "added_calls": added_calls,
                    "changed_defs": changed_defs,
                    "def_bodies_after": def_bodies_after,
                    "def_bodies_before": def_bodies_before,
                    "reasons": reasons,
                }
            )

    finally:
        stop_event.set()
        spinner_thread.join()
        print(" " * 40, end="\r")

    # --- 7. 出力 ---
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"✅ 総ヒット数: {len(results)}")
    print(f"📄 保存先: {OUTPUT_FILE}")