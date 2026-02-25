# 1_dataset.py

# 1) OpenAI APIで REST API 誤用修正かどうかを判定し 1_dataset.jsonl に逐次保存
# 2) さらに commit_url ごとにまとめて 1_dataset.json に集約して保存

import json
import os
from collections import defaultdict
from typing import Any, List, Dict
from openai import OpenAI
from dotenv import load_dotenv

REST_API = "SwitchBot API"
# REST_API = "Fitbit API"
# REST_API = "Philips Hue API"

INPUT_FILE = "0_filtered.json"
OUTPUT_JSONL = "1_dataset.jsonl"
OUTPUT_GROUPED = "1_dataset.json"  # commit 単位にまとめたデータ

NUM_PER_BATCH = 300  # 一度に処理する件数

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が設定されていません。")

OPENAI_RESPONSES_MODEL = os.getenv("OPENAI_RESPONSES_MODEL", "gpt-5.1")

client = OpenAI(api_key=OPENAI_API_KEY)


def analyze_rest_misuse_fix(before: str, after: str) -> tuple[bool, str]:
    prompt = f"""
System: You are provided with two code snippets: the original version ('Before Code') and the modified version ('After Code').
Your task is to decide if the modification fixes a critical misuse of the {REST_API}.
Focus only on the parts of the code that interact with the official {REST_API}, and determine whether the change addresses improper or incorrect API use.

Examples of critical {REST_API} misuses include, but are not limited to:
- Using an incorrect {REST_API} endpoint (URL)
- Using the wrong HTTP method for an API call
- Not including a required timeout in an API request
- Failing to handle exceptions when calling the API
- Any other significant issue that could cause the {REST_API} request to fail or behave incorrectly

Answer Criteria:
- Respond **Yes** if any modification fixes a critical misuse of the {REST_API} that would otherwise result in a runtime error, incorrect API behavior, invalid data transmission, or a failure to make a valid request to the {REST_API} servers.
- Respond **No** if the changes only enhance code robustness, readability, security, structure (such as adding logging or renaming variables), affect unrelated code, or the original code already handled interactions with the {REST_API} correctly.
- If multiple changes are present, respond **Yes** if at least one addresses a critical misuse of the {REST_API}.

---
Before Code:
{before}

After Code:
{after}

Output Format:
Provide your answer in plain text only as follows:
First line: Yes or No
Following line(s): A concise explanation (1–2 sentences) justifying your answer. Limit your response to a maximum of 3 sentences.
"""

    try:
        response = client.responses.create(
            model=OPENAI_RESPONSES_MODEL,
            input=prompt.strip(),
            reasoning={"effort": "high"},
            text={"verbosity": "low"},
        )

        # SDK バージョン差をケア
        content = (getattr(response, "output_text", "") or "").strip()
        if not content and getattr(response, "output", None):
            try:
                content = response.output[0].content[0].text.value.strip()
            except Exception:
                content = ""

        if not content:
            return (False, "空のレスポンス")

        lines = content.splitlines()
        answer = lines[0].strip().lower()
        reason = "\n".join(line.strip() for line in lines[1:] if line.strip()) or "理由なし"
        return ("yes" in answer, reason)
    except Exception as e:
        print(f"⚠️ OpenAI APIエラー: {e}")
        return (False, "OpenAI APIエラー")


def group_entries_by_commit(entries: List[Dict[str, Any]]) -> List[dict]:
    """
    commit_url ごとに {"commit_url": ..., "edits": [...]} の形にまとめる。
    """
    grouped: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"commit_url": None, "edits": []})

    for entry in entries:
        commit_url = entry.get("commit_url")
        if not commit_url:
            continue

        # edits には commit_url を含めない
        entry_without_commit = dict(entry)
        entry_without_commit.pop("commit_url", None)

        group = grouped[commit_url]
        group["commit_url"] = commit_url
        group["edits"].append(entry_without_commit)

    return list(grouped.values())


def save_json(data: Any, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"📄 保存先: {path}")


# ===＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝

def main() -> None:
    # 1) 0_filter.py の結果を読み込み
    with open(INPUT_FILE, encoding="utf-8") as f:
        data = json.load(f)

    # 2) def_map / call_map を構築
    def_map = {}
    call_map = {}

    for entry in data:
        path_before = entry["path_before"]
        path_after = entry["path_after"]

        # 関数定義
        for fname in entry.get("changed_defs", []):
            key = (path_before, path_after, fname)
            def_map[key] = {
                "code_before": entry.get("def_bodies_before", {}).get(fname, {}),
                "code_after": entry.get("def_bodies_after", {}).get(fname, {}),
            }

        # 呼び出し文（call + 行番号付き）
        key2 = (path_before, path_after)
        call_map[key2] = {
            "removed": entry.get("removed_calls", []),  # list of {"call", "line"}
            "added": entry.get("added_calls", []),
        }

    step3_data = data
    total = len(step3_data)
    current_idx = 0

    # 3) 出力 JSONL は毎回上書き開始（追記で積み重ならないように）
    with open(OUTPUT_JSONL, "w", encoding="utf-8") as f:
        pass  # 空ファイルを作成

    # 4) OpenAI 判定結果を一時的にメモリにも保持（後で commit ごとにまとめる用）
    judged_entries: List[Dict[str, Any]] = []

    while current_idx < total:
        batch_start = current_idx
        batch_end = min(current_idx + NUM_PER_BATCH, total)
        batch_size = batch_end - batch_start

        print(
            f"\n🔄 次の {batch_size} 件を処理します。（{batch_start+1}〜{batch_end} / {total}）Enterで開始 →"
        )
        input()

        for idx in range(batch_start, batch_end):
            entry = step3_data[idx]
            print(f"\n--- [{idx+1}/{total}] {entry['path_before']} ---")

            path_before = entry["path_before"]
            path_after = entry["path_after"]
            commit_url = entry.get("commit_url")
            file_name = entry.get("file_name")
            reasons = entry.get("reasons", [])

            for reason in reasons:
                # 1) 関数定義の差分を判定
                if reason["type"] in ["same_name_function_changed", "new_or_renamed_function"]:
                    fname = reason["function"]
                    key = (path_before, path_after, fname)
                    if key not in def_map:
                        print(f"⚠️ def_map に該当なし: {key}")
                        continue
                    func_before = def_map[key]["code_before"]
                    func_after = def_map[key]["code_after"]

                    code_before = func_before.get("code", "")
                    code_after = func_after.get("code", "")

                    print(f"🔍 関数定義 '{fname}' を判定中...")
                    is_fix, reason_text = analyze_rest_misuse_fix(code_before, code_after)

                    if is_fix:
                        # ★ ここで comment / isEnabled を type の前に入れる
                        result = {
                            "comment": "",
                            "isEnabled": "",
                            "type": "def_fix",
                            "path_before": path_before,
                            "path_after": path_after,
                            "function": fname,
                            "code_before": code_before,
                            "code_after": code_after,
                            "reason": reason_text,
                            "commit_url": commit_url,
                            "file_name": file_name,
                            "function_lineno": {
                                "before": func_before.get("start_line"),
                                "after": func_after.get("start_line"),
                            },
                        }
                        # JSONL に追記
                        with open(OUTPUT_JSONL, "a", encoding="utf-8") as f:
                            f.write(json.dumps(result, ensure_ascii=False) + "\n")
                        # メモリにも保持
                        judged_entries.append(result)

                        print(f"✅ 誤用修正と判定\n📝 理由: {reason_text}")
                    else:
                        print(f"❌ 誤用修正ではない\n📝 理由: {reason_text}")

                # 2) REST 呼び出し追加 / 変更の差分を判定
                elif reason["type"] == "rest_api_call_added":
                    call_info = call_map.get((path_before, path_after))
                    if not call_info:
                        print(f"⚠️ call_map に該当なし: {(path_before, path_after)}")
                        continue

                    removed_calls = call_info.get("removed") or []
                    added_calls = call_info.get("added") or []

                    if not added_calls:
                        print("⚠️ added_calls が空のためスキップ")
                        continue

                    # 0_filter.py の reasons に入っている call と一致するものを優先して選ぶ
                    target_call_text = reason.get("call")
                    matching_added = [
                        c for c in added_calls if c.get("call") == target_call_text
                    ]
                    after_call = (matching_added or added_calls)[0]

                    # removed_calls が空の場合は「元々は呼び出しがなかった」とみなして空文字列で評価
                    if removed_calls:
                        before_call = removed_calls[0]
                    else:
                        before_call = {"call": "", "line": None}

                    print(
                        "🔍 関数呼び出しを判定中..."
                        f"\nBefore: {before_call['call']}\nAfter : {after_call['call']}"
                    )

                    is_fix, reason_text = analyze_rest_misuse_fix(
                        before_call["call"], after_call["call"]
                    )

                    if is_fix:
                        # ★ ここも同様に comment / isEnabled を先に
                        result = {
                            "comment": "",
                            "isEnabled": "",
                            "type": "call_fix",
                            "path_before": path_before,
                            "path_after": path_after,
                            "call_before": before_call["call"],
                            "call_after": after_call["call"],
                            "reason": reason_text,
                            "commit_url": commit_url,
                            "file_name": file_name,
                            "call_lineno": {
                                "before": before_call.get("line"),
                                "after": after_call.get("line"),
                            },
                        }
                        # JSONL に追記
                        with open(OUTPUT_JSONL, "a", encoding="utf-8") as f:
                            f.write(json.dumps(result, ensure_ascii=False) + "\n")
                        # メモリにも保持
                        judged_entries.append(result)

                        print(f"✅ 誤用修正と判定\n📝 理由: {reason_text}")
                    else:
                        print(f"❌ 誤用修正ではない\n📝 理由: {reason_text}")

        current_idx = batch_end

    print("✅ 全てのデータを処理済みです。")

    # 5) commit_url ごとにまとめて 1_dataset.json に保存
    grouped_data = group_entries_by_commit(judged_entries)
    print(f"✅ 総コミット数: {len(grouped_data)}")
    save_json(grouped_data, OUTPUT_GROUPED)


if __name__ == "__main__":
    main()