# 2_context.py

# 1) 修正例をカテゴリ分類＋コンテキスト生成して 2_context.jsonl に保存
# 2) 2_context.jsonl を集約して 2_context.json を生成

import os
import json
import re
import hashlib
from typing import Any, Dict, List, Set
from collections import Counter
from openai import OpenAI
from dotenv import load_dotenv

INPUT_FILE = "1_dataset(SB).json"
STEP7_JSONL = "2_context.jsonl"
STEP8_JSON = "2_context.json" # 集約: コンテキスト一覧
NUM_PER_BATCH = 100

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が設定されていません。")

OPENAI_RESPONSES_MODEL = os.getenv("OPENAI_RESPONSES_MODEL", "gpt-5.1")

client = OpenAI(api_key=OPENAI_API_KEY)


def hash_key(before: str, after: str) -> str:
    """before/after のスニペットから一意キーを生成"""
    data = before + "||" + after
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def get_context_template(category: str) -> str:
    templates = {
        "1": """To ensure compatibility with the latest API specifications, verify the following:
1) Use the most up-to-date endpoint URL. Deprecated or outdated endpoints may cause failures in API calls.
2) Confirm that the new endpoint supports the required operations for your request.""",

        "2": """To successfully authenticate and authorize API requests, review the following:
1) Ensure that the necessary authentication headers (e.g., API keys, tokens) are included and valid.
2) Confirm that any required headers (such as Content-Type or custom keys) are formatted correctly for the target API.""",

        "3": """To align with the intended API behavior, please ensure the following:
1) The correct HTTP method (GET, POST, PUT, DELETE, etc.) is being used for the given endpoint.
2) Review the API documentation to confirm method-specific constraints or requirements.""",

        "4": """To meet the API’s expected input format, check the following:
1) All required parameters are present and correctly named.
2) The data structure of the payload or query parameters matches the API specification, including nested fields or object types.""",

        "5": """To handle potential delays or unresponsive endpoints effectively:
1) Set an appropriate timeout for the API call based on expected response times.
2) Implement fallback or retry logic as needed to ensure robust error handling during timeouts.""",

        "6": """To properly process API responses:
1) Check the status codes returned (e.g., 200, 400, 500) and implement conditional logic accordingly.
2) Parse the response body safely and validate the presence of expected fields or values.""",

        "7": """To improve the resilience of the API integration:
1) Add try-except blocks or equivalent error handling to manage unexpected failures.
2) Log or surface meaningful error messages to assist with debugging and issue tracking.""",

        "8": """Review the applied changes carefully to ensure consistency with the intended API behavior. Consider both functional correctness and alignment with coding best practices."""
    }

    if category not in templates:
        print(f"⚠️ 未知のカテゴリ '{category}' を検出。カテゴリ8を使用します。")
        category = "8"

    return templates[category]


def build_classification_prompt(before: str, after: str) -> str:
    return f"""
System: Classify the following code modification by selecting the most appropriate category number from the list below.

Choices (Output only the number 1–8 on the first line):
1. Endpoint fix or modification (e.g., changes to URL or API path)
2. Header / Authentication fix or modification (e.g., changes or additions to auth tokens or headers)
3. HTTP method fix or modification (e.g., GET → POST)
4. Parameter / structure fix or modification (e.g., API parameter names or request structure)
5. Timeout handling fix or modification (e.g., timeout settings or retry logic)
6. Response handling fix or modification (e.g., handling status codes or processing response data)
7. Exception handling fix or modification (e.g., try-except blocks or error handling)
8. Other (modification does not fit into the above categories)

---
Before Code:
{before}

After Code:
{after}

Output Format:
Provide your answer in plain text only as follows:
First line: a single number (1–8) that represents the selected category.
""".strip()


def extract_category(response_text: str) -> str:
    lines = [line.strip() for line in response_text.splitlines() if line.strip()]
    if not lines:
        return "8"
    match = re.match(r"^([1-8])(?:\D|$)", lines[0])
    if match:
        return match.group(1)
    else:
        print(f"⚠️ 想定外のカテゴリ表現: '{lines[0]}'. '8'（その他）に設定します。")
        return "8"


def load_existing_keys() -> Set[str]:
    """既に 2_context.jsonl に書き出された key を読み込んで重複実行を避ける"""
    if not os.path.exists(STEP7_JSONL):
        return set()
    existing: Set[str] = set()
    with open(STEP7_JSONL, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
                key = obj.get("key")
                if key:
                    existing.add(key)
            except json.JSONDecodeError:
                continue
    return existing


def read_file_safely(path: str) -> str:
    """path_before/path_after のパスからファイル内容を安全に読み込む"""
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception as e:
        print(f"⚠️ ファイル読み込みエラー ({path}): {e}")
        return ""


def flatten_edits(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for entry in data:
        for edit in entry.get("edits", []):
            # isEnabled が無ければ "false" とみなす（= 手動で有効化したものだけ使う想定）
            is_enabled = str(edit.get("isEnabled", "false")).lower()
            if is_enabled not in ["true", "maybe true"]:
                continue  # 対象外はスキップ

            edit_type = edit.get("type", "")
            if edit_type == "def_fix":
                before = edit.get("code_before", "")
                after = edit.get("code_after", "")
                cot = edit.get("reason", "")
            elif edit_type == "call_fix":
                before = edit.get("call_before", "")
                after = edit.get("call_after", "")
                cot = edit.get("reason", "")
            else:
                continue

            if before and after:
                result.append(
                    {
                        "before": before,                 # スニペット（差分対象）
                        "after": after,                   # スニペット（差分対象）
                        "cot": cot,
                        "path_before": edit.get("path_before"),  # 元ファイルパス (before)
                        "path_after": edit.get("path_after"),    # 元ファイルパス (after)
                    }
                )
    return result


def append_step7_jsonl(data: Dict[str, Any]) -> None:
    with open(STEP7_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def summarize_step7_to_step8(input_path: str, output_path: str) -> None:
    if not os.path.exists(input_path):
        print(f"⚠️ {input_path} が存在しないためサマリ生成をスキップします。")
        return

    simplified_data: List[Dict[str, Any]] = []
    category_counter: Counter = Counter()

    with open(input_path, "r", encoding="utf-8") as infile:
        for line in infile:
            if not line.strip():
                continue  # 空行はスキップ

            try:
                obj = json.loads(line)

                category = obj.get("category", "8")
                context = obj.get("context", "")
                context_with_original = obj.get("context_with_original", "")
                context_simple = obj.get("context_simple", "")
                context_simple_with_original = obj.get("context_simple_with_original", "")

                simplified_data.append({
                    "category": category,
                    "context": context,
                    "context_with_original": context_with_original,
                    "context_simple": context_simple,
                    "context_simple_with_original": context_simple_with_original,
                })

                category_counter[category] += 1

            except json.JSONDecodeError as e:
                print(f"⚠️ JSONデコードエラー: {e}")

    # JSONとして保存
    with open(output_path, "w", encoding="utf-8") as outfile:
        json.dump(simplified_data, outfile, ensure_ascii=False, indent=2)

    print("\n📊 カテゴリ別の件数:")

    def sort_key(x: str):
        try:
            return (0, int(x))  # 数値カテゴリを優先 & 数値でソート
        except ValueError:
            return (1, x)       # それ以外は後ろに文字列順で

    for category in sorted(category_counter.keys(), key=sort_key):
        print(f"  - カテゴリ {category}: {category_counter[category]} 件")
    
    print(f"\n📄 保存先: {output_path}")


# ===＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝

def main() -> None:
    # 1) 1_dataset.json を読み込み
    with open(INPUT_FILE, encoding="utf-8") as f:
        raw_data = json.load(f)

    entries = flatten_edits(raw_data)
    print(f"flatten_edits で得られたエントリ数: {len(entries)}")

    indexed_entries = list(enumerate(entries))  # 元の順番を保持

    while True:
        done_keys = load_existing_keys()
        candidates = [
            (i, e)
            for i, e in indexed_entries
            if hash_key(e["before"], e["after"]) not in done_keys
        ]

        if not candidates:
            print("✅ 全てのデータを分類済みです。")
            break

        print(
            f"\n🔄 残り {len(candidates)} 件。Enter を押すと最大 {NUM_PER_BATCH} 件を分類します。"
        )
        input()

        batch = candidates[:NUM_PER_BATCH]
        print(f"\n📦 {len(batch)} 件の分類処理を開始します。\n")

        for local_idx, (global_idx, entry) in enumerate(batch, 1):
            before = entry["before"]          # スニペット(before)
            after = entry["after"]           # スニペット(after)
            cot = entry["cot"]
            path_before = entry.get("path_before")
            path_after = entry.get("path_after")

            # ハッシュキーはスニペットから生成（重複判定用）
            key = hash_key(before, after)

            # 元ファイルの中身を読み込む
            original_before = read_file_safely(path_before)
            original_after = read_file_safely(path_after)

            print(f"\n📝 [{global_idx + 1}/{len(entries)}] API分類処理中...")

            classification_prompt = build_classification_prompt(before, after)

            try:
                # Responses API を使用
                response = client.responses.create(
                    model=OPENAI_RESPONSES_MODEL,
                    input=classification_prompt.strip(),
                    reasoning={"effort": "high"},
                    text={"verbosity": "low"}
                )

                # SDK のバージョンによっては output_text ではなく output[...] になる場合があり
                content = (getattr(response, "output_text", "") or "").strip()
                if not content and getattr(response, "output", None):
                    try:
                        content = response.output[0].content[0].text.value.strip()
                    except Exception:
                        content = ""

                if not content:
                    print("⚠️ 空のレスポンスを受信しました。カテゴリ8として扱います。")
                    category = "8"
                else:
                    category = extract_category(content)

                print(f"✅ 分類結果: カテゴリ={category}")

                instruction = get_context_template(category)

                # --- 1) 元コードを含まないコンテキスト（コンパクト） ---
                context = f"""Code before modification:
{before}

{instruction}

Code after modification:
{after}

The following issue(s) were addressed this time:
{cot}
"""

                # --- 2) 元コードを含むコンテキスト（リッチ） ---
                context_with_original = f"""Code before modification (target snippet):
{before}

Original file before modification ({path_before or 'N/A'}):
{original_before}

{instruction}

Code after modification (target snippet):
{after}

Original file after modification ({path_after or 'N/A'}):
{original_after}

The following issue(s) were addressed this time:
{cot}
"""

                # --- 3) シンプル版（スニペットのみ） ---
                context_simple = f"""Code before modification:
{before}

Code after modification:
{after}
"""

                # --- 4) シンプル＋元コード版 ---
                context_simple_with_original = f"""Code before modification (target snippet):
{before}

Original file before modification ({path_before or 'N/A'}):
{original_before}

Code after modification (target snippet):
{after}

Original file after modification ({path_after or 'N/A'}):
{original_after}
"""

                result_entry = {
                    "key": key,
                    "category": category,
                    "context": context,
                    "context_with_original": context_with_original,
                    "context_simple": context_simple,
                    "context_simple_with_original": context_simple_with_original,
                }

                append_step7_jsonl(result_entry)

            except Exception as e:
                print(f"❌ API呼び出しエラー: {e}")

    summarize_step7_to_step8(STEP7_JSONL, STEP8_JSON)


if __name__ == "__main__":
    main()