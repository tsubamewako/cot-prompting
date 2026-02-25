# 3_prompt.py

# 1) 修正対象ファイルをターミナルで選択させ，
# 2) 同一プロンプトを複数回 OpenAI API に投げた結果を
# 3) ../result下に <プロンプト形式タグ>_（n）.<ext> で保存する

import os
import json
import random
import re
import requests
from typing import List, Tuple, Optional, Dict
from openai import OpenAI
from dotenv import load_dotenv

CONTEXT_FILE = "2_context.json"
DATASET_DIR = "../dataset"
OUTPUT_DIR = "../result"


# プロンプト形式タグ: "FC"（Few-shot CoT） / "F"（Few-shot） / "Z"（Zero-shot）
PROMPT_TYPE_TAG = "FC"

# どのカテゴリのコンテキスト(修正ペア)をいくつ含めるか:
CONTEXT_COUNTS: Dict[str, int] = {
    "1": 3,  # エンドポイント
    "2": 3,  # ヘッダー / 認証情報
    "3": 1,  # HTTPメソッド
    "4": 3,  # パラメータ / 構造
    "5": 0,  # タイムアウト処理
    "6": 0,  # レスポンス処理
    "7": 0,  # 例外処理
    "8": 0,  # その他
}

# コンテキストに元コード（before/after のファイル全体）を含めるかどうか
USE_ORIGINAL_CONTEXT = False

# 同じプロンプトを各ケースに対して何回実行するか（1以上の整数）
NUM_RUNS_PER_CASE = 5

# ローカルLLM実行モード
local = True
# VPN経由でローカルLLMに接続する場合はTrueに設定
use_vpn = False


load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_RESPONSES_MODEL = os.getenv("OPENAI_RESPONSES_MODEL", "gpt-5.1")

if not local and not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY が設定されていません。")

client = OpenAI(api_key=OPENAI_API_KEY) if not local else None

_default_llm_host = "192.168.30.10" if use_vpn else "192.168.1.10"
LOCAL_LLM_HOST = os.getenv("LOCAL_LLM_HOST", _default_llm_host)
LOCAL_LLM_PORT = os.getenv("LOCAL_LLM_PORT", "1234")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "qwen/qwen3-coder-480b")
LOCAL_LLM_URL = os.getenv(
    "LOCAL_LLM_URL",
    f"http://{LOCAL_LLM_HOST}:{LOCAL_LLM_PORT}/v1/chat/completions",
)

# --- プロンプトテンプレート ---
TEMPLATE_FEWSHOT = """
System: You are an AI assistant focused on ensuring proper usage of REST APIs.

Instructions:
- Audit the **Input Data** for any incorrect or inappropriate use of REST APIs.
- Use the **Contexts** below as authoritative references or examples.
- Make all necessary corrections within **Input Data** to address potential misuses.

Contexts:
{context}

Input Data:
{input_data}

Output Indicator:
- Return the full, modified code.
- Rules:
  1. Maintain the original structure and formatting of **Input Data** wherever possible.
  2. Only if you make changes, mark all modified lines with a `✅` to highlight your edits.

Output Verbosity:
- Limit your response to the edited code only.
- Keep modifications minimal and concrete.
- Do not include explanations, summaries, or extra commentary. Prioritize complete, actionable corrections, but do not exceed the code block itself.
"""

TEMPLATE_ZEROSHOT = """
System: You are an AI assistant focused on ensuring proper usage of REST APIs.

Instructions:
- Audit the **Input Data** for any incorrect or inappropriate use of REST APIs.
- Make all necessary corrections within **Input Data** to address potential misuses.

Input Data:
{input_data}

Output Indicator:
- Return the full, modified code.
- Rules:
  1. Maintain the original structure and formatting of **Input Data** wherever possible.
  2. Only if you make changes, mark all modified lines with a `✅` to highlight your edits.

Output Verbosity:
- Limit your response to the edited code only.
- Keep modifications minimal and concrete.
- Do not include explanations, summaries, or extra commentary. Prioritize complete, actionable corrections, but do not exceed the code block itself.
"""


def load_context_entries(filepath: str) -> List[Tuple[str, str, str, str, str]]:
    """
    2_context.json を読み込み、(category, context, context_with_original,
    context_simple, context_simple_with_original) のタプルのリストを返す。
    """
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    entries: List[Tuple[str, str, str, str, str]] = []
    for item in data:
        cat = str(item.get("category"))
        ctx = item.get("context", "")
        ctx_with_orig = item.get("context_with_original", "")
        ctx_simple = item.get("context_simple", "")
        ctx_simple_with_orig = item.get("context_simple_with_original", "")
        if cat:
            entries.append((cat, ctx, ctx_with_orig, ctx_simple, ctx_simple_with_orig))
    return entries


def group_entries_by_category(
    entries: List[Tuple[str, str, str, str, str]]
) -> Dict[str, List[Tuple[str, str, str, str, str]]]:
    """
    entries をカテゴリごとにグループ化する。
    """
    grouped: Dict[str, List[Tuple[str, str, str, str, str]]] = {}
    for e in entries:
        cat = e[0]
        grouped.setdefault(cat, []).append(e)
    return grouped


def sample_contexts_by_counts(
    all_entries: List[Tuple[str, str, str, str, str]],
    context_counts: Dict[str, int],
) -> List[Tuple[str, str, str, str, str]]:
    """
    CONTEXT_COUNTS の指定に基づいてカテゴリごとにコンテキストをサンプリングする。
    - context_counts: {"1": 3, "3": 2, ...}
    戻り値: 選ばれた entries のリスト（カテゴリは混在・シャッフル）。
    """
    grouped = group_entries_by_category(all_entries)
    selected: List[Tuple[str, str, str, str, str]] = []

    for cat, requested in context_counts.items():
        if requested <= 0:
            continue
        pool = grouped.get(cat, [])
        if not pool:
            print(f"⚠️ カテゴリ {cat} に該当するコンテキストがありません。")
            continue

        if requested >= len(pool):
            pool_copy = pool[:]
            random.shuffle(pool_copy)
            chosen = pool_copy
        else:
            chosen = random.sample(pool, requested)

        selected.extend(chosen)

    random.shuffle(selected)
    return selected


def format_contexts(
    entries: List[Tuple[str, str, str, str, str]],
    use_simple: bool,
    use_original: bool,
) -> str:
    """
    entries: (category, context, context_with_original,
              context_simple, context_simple_with_original)
    """
    formatted = []
    for idx, (cat, ctx, ctx_with_orig, ctx_simple, ctx_simple_with_orig) in enumerate(
        entries, 1
    ):
        if use_simple and use_original:
            body = ctx_simple_with_orig
        elif use_simple and not use_original:
            body = ctx_simple
        elif not use_simple and use_original:
            body = ctx_with_orig
        else:
            body = ctx
        body = (body or "").strip()
        formatted.append(f"##### Example {idx} (Category {cat})\n{body}")
    return "\n\n".join(formatted)


def escape_braces(s: str) -> str:
    """
    str.format 用に { と } をエスケープする（コード内の JSON / f-string などに対応）
    """
    return s.replace("{", "{{").replace("}", "}}")


def generate_prompt(
    template: str,
    user_code: str,
    context_block: Optional[str] = None,
) -> str:
    code_escaped = escape_braces(user_code.strip())
    if context_block is not None:
        ctx_escaped = escape_braces(context_block)
        return template.format(context=ctx_escaped, input_data=code_escaped)
    else:
        return template.format(input_data=code_escaped)


def strip_markdown_code_fence(text: str) -> str:
    """
    ```python ... ``` や ``` ... ``` で囲まれている場合、
    フェンスを取り除き、中身だけを返す。
    それ以外の場合は text をそのまま返す。
    """
    text = text.strip()
    # ```python\n ... \n``` / ```\n ... \n``` の両方に対応
    m = re.match(r"^```(?:[a-zA-Z0-9_+\-]+)?\s*\n(.*?)\n```$", text, re.DOTALL)
    if m:
        inner = m.group(1)
        return inner.strip("\n")
    return text


def call_local_llm_with_prompt(prompt: str) -> str:
    """
    ローカル LLM API にプロンプトを投げて、テキスト出力だけを返す。
    もし ```python ... ``` 等のコードブロックで返ってきた場合は、
    フェンスを剥がしてソースコード部分だけを返す。
    """
    headers = {
        "Content-Type": "application/json",
    }
    payload = {
        "model": LOCAL_LLM_MODEL,
        "messages": [
            {"role": "user", "content": prompt.strip()}
        ],
        "stream": False
    }

    response = requests.post(LOCAL_LLM_URL, headers=headers, data=json.dumps(payload))
    response.raise_for_status()
    resp_json = response.json()

    raw = ""
    try:
        raw = resp_json["choices"][0]["message"]["content"].strip()
    except Exception:
        raw = ""

    # ここで ```python ... ``` を剥がす
    return strip_markdown_code_fence(raw)



def call_openai_with_prompt(prompt: str) -> str:
    """
    Responses API にプロンプトを投げて、テキスト出力だけを返す。
    もし ```python ... ``` 等のコードブロックで返ってきた場合は、
    フェンスを剥がしてソースコード部分だけを返す。
    """
    if client is None:
        raise RuntimeError("OpenAI クライアントが構成されていません（local=True）。")

    resp = client.responses.create(
        model=OPENAI_RESPONSES_MODEL,
        input=prompt.strip(),
        reasoning={"effort": "medium"},
        text={"verbosity": "medium"},
    )

    raw = (getattr(resp, "output_text", "") or "").strip()
    if not raw and getattr(resp, "output", None):
        try:
            raw = resp.output[0].content[0].text.value.strip()
        except Exception:
            raw = ""

    # ここで ```python ... ``` を剥がす
    return strip_markdown_code_fence(raw)


def build_prompt_label(prompt_type_tag: str, use_original: bool) -> str:
    tag = prompt_type_tag.upper()
    if tag == "Z":
        return "Z"
    if use_original:
        return f"<{tag}>"
    return tag


def save_repaired_code(
    case_id: str,
    src_path: str,
    code: str,
    prompt_type_tag: str,
    use_original: bool,
    run_index: int,
) -> str:

    case_dir = os.path.join(OUTPUT_DIR, case_id)
    os.makedirs(case_dir, exist_ok=True)

    _, ext = os.path.splitext(src_path)
    if not ext:
        ext = ".txt"

    label = build_prompt_label(prompt_type_tag, use_original)
    run_str = f"（{run_index}）"  # 全角カッコで (n)

    out_name = f"{label}_{run_str}{ext}"
    out_path = os.path.join(case_dir, out_name)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(code)

    return out_path


def list_apis() -> List[str]:
    """
    ../dataset 配下の API ディレクトリ名一覧を返す。
    例: ["Fitbit", "SwitchBot"]
    """
    if not os.path.isdir(DATASET_DIR):
        return []
    apis = [
        d for d in os.listdir(DATASET_DIR)
        if os.path.isdir(os.path.join(DATASET_DIR, d))
    ]
    apis.sort()
    return apis


def ask_api_name() -> Optional[str]:
    """
    ターミナル上で API ディレクトリを 1つ選ばせる。
    戻り値: 選択された API 名（例: "Fitbit"） or None
    """
    apis = list_apis()
    if not apis:
        print(f"❌ {DATASET_DIR} 配下に API ディレクトリが見つかりません。")
        return None

    print("\n🔍 対象ディレクトリを選択してください:")
    for i, name in enumerate(apis, 1):
        print(f"{i}: {name}")

    while True:
        s = input("番号: ").strip()
        if not s.isdigit():
            print("⚠️ 数字で入力してください。")
            continue
        idx = int(s)
        if not (1 <= idx <= len(apis)):
            print(f"⚠️ 1〜{len(apis)} の範囲で選択してください。")
            continue
        return apis[idx - 1]


def list_misuse_cases_for_api(api_name: str) -> Dict[str, str]:
    """
    ../dataset/API/数字/ 配下で
      - misuse.<ext>
      - misuse(n).<ext>
      - misuse (n).<ext>
      - misuse（n）.<ext>
      - misuse （n）.<ext>
    のいずれかを探し、{ case_id: misuse_full_path } を返す。
    """
    cases: Dict[str, str] = {}

    api_dir = os.path.join(DATASET_DIR, api_name)
    if not os.path.isdir(api_dir):
        return cases

    for case_id in os.listdir(api_dir):
        case_dir = os.path.join(api_dir, case_id)
        if not os.path.isdir(case_dir):
            continue

        candidates: List[str] = []
        for fname in os.listdir(case_dir):
            # misuse.<ext>
            if fname.startswith("misuse."):
                candidates.append(os.path.join(case_dir, fname))
                continue
            # misuse(n).<ext> / misuse (n).<ext> / misuse（n）.<ext> / misuse （n）.<ext>
            if re.match(r"^misuse\s*[（(]\d+[）)]\.", fname):
                candidates.append(os.path.join(case_dir, fname))

        if not candidates:
            continue

        # misuse.<ext> を優先し、それがなければ misuse*(n).<ext> のうち名前順で先のものを使う
        candidates.sort(
            key=lambda p: (
                0 if os.path.basename(p).startswith("misuse.") else 1,
                os.path.basename(p),
            )
        )
        misuse_file = candidates[0]
        cases[case_id] = misuse_file

    return cases


def ask_target_cases_for_api(api_name: str) -> List[Tuple[str, str]]:
    """
    指定された API 配下の 数字/misuse*. <ext> を一覧表示し，
    「数字（case_id）」を選択させる。
    戻り値: (case_id, misuse_full_path) のリスト。
    """
    cases = list_misuse_cases_for_api(api_name)
    if not cases:
        print(f"❌ {DATASET_DIR}/{api_name} 配下に misuse*. <ext> を含むケースが見つかりません。")
        return []

    sorted_ids = sorted(cases.keys(), key=lambda x: (len(x), x))

    print(f"\n🔍 対象ファイルを選択してください:")
    for cid in sorted_ids:
        misuse_path = cases[cid]
        rel = os.path.relpath(misuse_path, DATASET_DIR)
        print(f"ケースID {cid}: {rel}")

    print("\n  - カンマ区切りでケースIDを指定:  1,3,5")
    print("  - このAPIのすべてのケースを対象にする:  all")

    while True:
        s = input("ケースID: ").strip()
        if not s:
            print("⚠️ 入力が空です。再入力してください。")
            continue

        s_lower = s.lower()
        if s_lower == "all":
            return [(cid, cases[cid]) for cid in sorted_ids]

        parts = [p.strip() for p in s.split(",") if p.strip()]
        selected: List[Tuple[str, str]] = []
        ok = True
        for p in parts:
            if p not in cases:
                print(f"⚠️ ケースID '{p}' は存在しません。")
                ok = False
                break
            selected.append((p, cases[p]))
        if not ok:
            continue

        # case_id の重複を避けて返却
        unique: Dict[str, str] = {}
        for cid, path in selected:
            unique[cid] = path

        return list(unique.items())


# ===＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝＝

def main() -> None:
    # 1) コンテキストを読み込み
    if not os.path.exists(CONTEXT_FILE):
        print(f"❌ コンテキストファイルが見つかりません: {CONTEXT_FILE}")
        return

    all_entries = load_context_entries(CONTEXT_FILE)
    if not all_entries:
        print(f"❌ コンテキストが空です: {CONTEXT_FILE}")
        return

    # 2) 設定値のチェック・反映
    tag = PROMPT_TYPE_TAG.upper()
    if tag == "FC":
        prompt_type = "Few-shot CoT"
    elif tag == "F":
        prompt_type = "Few-shot"
    elif tag == "Z":
        prompt_type = "Zero-shot"
    else:
        print(f"⚠️ 不正な PROMPT_TYPE_TAG: {PROMPT_TYPE_TAG} → Z (Zero-shot) にフォールバックします。")
        tag = "Z"
        prompt_type = "Zero-shot"

    if NUM_RUNS_PER_CASE <= 0:
        print(f"⚠️ NUM_RUNS_PER_CASE が {NUM_RUNS_PER_CASE} のため、1以上を指定してください。")
        return

    print(f"使用するプロンプト形式: {prompt_type}")
    print(f"各ケースにつき実行回数: {NUM_RUNS_PER_CASE} 回")

    # Zero-shot 以外のときだけコンテキストを使う
    use_context = tag != "Z"

    if use_context:
        if not CONTEXT_COUNTS:
            print("⚠️ CONTEXT_COUNTS が空のため、コンテキストが使用できません。処理を終了します。")
            return

        print("使用するカテゴリと件数（要求値）：")
        for c, n in CONTEXT_COUNTS.items():
            print(f"  - カテゴリ {c}: {n} 件")

        grouped = group_entries_by_category(all_entries)
        total_available = 0
        for c, n in CONTEXT_COUNTS.items():
            total_available += min(n, len(grouped.get(c, [])))

        if total_available == 0:
            print("❌ CONTEXT_COUNTS に該当するコンテキストが1件もありません。処理を終了します。")
            return

        print(f"元コードの有無: {'有' if USE_ORIGINAL_CONTEXT else '無'}")
    else:
        print("コンテキストは使用しません（Zero-shot）。")

    # 3) API を選択
    api_name = ask_api_name()
    if not api_name:
        return

    # 4) 選択した API の中から修正対象ケースを選択
    target_cases = ask_target_cases_for_api(api_name)
    if not target_cases:
        print("⚠️ 修正対象ケースが選択されなかったため終了します。")
        return

    # 5) 各ケースに対して処理
    print("\n📦 修正処理を開始します:")
    for case_id, misuse_path in target_cases:
        print(f"\n--- ケースID {case_id}（{misuse_path}） ---")
        try:
            with open(misuse_path, "r", encoding="utf-8") as f:
                user_code = f.read()
        except Exception as e:
            print(f"⚠️ ファイル読み込みエラー ({misuse_path}): {e}")
            continue

        # --- プロンプトはこのケースにつき 1 回だけ生成 ---
        if use_context:
            sampled = sample_contexts_by_counts(all_entries, CONTEXT_COUNTS)
            if not sampled:
                print("⚠️ コンテキストが取得できませんでした。このケースはスキップします。")
                continue
            use_simple = (tag == "F")
            ctx_block = format_contexts(
                sampled,
                use_simple=use_simple,
                use_original=USE_ORIGINAL_CONTEXT,
            )
            prompt = generate_prompt(TEMPLATE_FEWSHOT, user_code, ctx_block)
        else:
            prompt = generate_prompt(TEMPLATE_ZEROSHOT, user_code)

        # --- 同じプロンプトを NUM_RUNS_PER_CASE 回実行 ---
        for run_index in range(1, NUM_RUNS_PER_CASE + 1):
            print(f"\n🧠 OpenAI API による修正を実行中...（ケースID {case_id}, {run_index} 回目）")
            try:
                if(local == True):
                    repaired_code = call_local_llm_with_prompt(prompt)
                else:
                    repaired_code = call_openai_with_prompt(prompt)

            except Exception as e:
                print(f"❌ OpenAI API 呼び出しエラー: {e}")
                break

            if not repaired_code:
                print("⚠️ 空のレスポンスを受信しました。このケースの残りの実行をスキップします。")
                break

            # 保存
            out_path = save_repaired_code(
                case_id,
                misuse_path,
                repaired_code,
                tag,
                USE_ORIGINAL_CONTEXT,
                run_index,
            )
            print(f"✅ 修正結果を保存しました: {out_path}")

    print("\n🎉 全ての対象ケースの処理が完了しました。")


if __name__ == "__main__":
    main()