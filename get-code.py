# githubからキーワードに関連するコードを取得し、patch形式で保存
import requests
import re
import os
import datetime
import itertools
import hashlib
import threading
import time
import warnings
from collections import defaultdict
from dotenv import load_dotenv

warnings.filterwarnings("ignore", category=UserWarning)

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if not GITHUB_TOKEN:
	raise RuntimeError("GITHUB_TOKEN が .env に設定されていません")

headers = {
	"Authorization": f"token {GITHUB_TOKEN}",
	"Accept": "application/vnd.github.v3+json"
}

extension_language_map = {
	".py": "Python",
	".java": "Java",
	".js": "JavaScript",
}
patch_counter = itertools.count(start=0)
seen_commit_urls = set()
seen_file_hashes = set()
lang_patch_counter = defaultdict(int)

# ファイル取得
def download_raw_file(owner, repo, sha, filepath):
	raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{filepath}"
	try:
		response = requests.get(raw_url, headers=headers, timeout=(5, 15))
		time.sleep(0.5)  # レート制限対策
		if response.status_code == 200:
			return response.text
		elif response.status_code in [403, 429]:
			print("⚠️ レート制限に達しました。しばらく待機してください。")
			time.sleep(60)
	except requests.RequestException:
		pass  # エラーは静かに処理
	return None

# コミットURLからbefore/afterペアを抽出
def fetchCmdata(cm_url, output_root):
	if cm_url in seen_commit_urls:
		return
	seen_commit_urls.add(cm_url)

	match = re.match(r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/commit/(?P<sha>[a-f0-9]{40})", cm_url)
	if not match:
		return

	owner, repo, sha = match.group("owner"), match.group("repo"), match.group("sha")
	url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}"

	try:
		response = requests.get(url, headers=headers, timeout=(5, 15))
		time.sleep(1)
		if response.status_code in [403, 429]:
			print("⚠️ レート制限に達しました。しばらく待機してください。")
			time.sleep(60)
			return
		if response.status_code != 200:
			return
		commit_details = response.json()
	except requests.RequestException:
		pass  # エラーは静かに処理
		return

	parent_sha = commit_details["parents"][0]["sha"] if commit_details["parents"] else None
	files = commit_details.get("files", [])
	if len(files) > 10:
		print(f"⏩ スキップ（ファイル数: {len(files)}）")
		return

	for file in files:
		filename = file["filename"]
		ext = os.path.splitext(filename)[1] or ".txt"
		lang = extension_language_map.get(ext)
		if not lang:
			continue

		before = download_raw_file(owner, repo, parent_sha, filename) if parent_sha else None
		after = download_raw_file(owner, repo, sha, filename)
		if before and after:
			file_pair_hash = hashlib.sha256((before + "||" + after).encode()).hexdigest()
			if file_pair_hash in seen_file_hashes:
				continue
			seen_file_hashes.add(file_pair_hash)

			patch_id = next(patch_counter)
			patch_dir = os.path.join(output_root, lang, f"patch{patch_id}")
			os.makedirs(patch_dir, exist_ok=True)
			lang_patch_counter[lang] += 1

			with open(os.path.join(patch_dir, f"before{ext}"), "w", encoding="utf-8") as f:
				f.write(before)
			with open(os.path.join(patch_dir, f"after{ext}"), "w", encoding="utf-8") as f:
				f.write(after)
			with open(os.path.join(patch_dir, "commit_url.txt"), "w", encoding="utf-8") as f:
				f.write(cm_url + "\n")
			with open(os.path.join(patch_dir, "file_name.txt"), "w", encoding="utf-8") as f:
				f.write(filename + "\n")

# プルリクURLからコミット検索
def fetchCms(pr_url, output_root):
	match = re.match(r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)", pr_url)
	if not match:
		return
	owner, repo, pr_number = match.group("owner"), match.group("repo"), match.group("number")
	url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/commits"

	while url:
		response = requests.get(url, headers=headers)
		time.sleep(1)  # レート制限対策
		if response.status_code in [403, 429]:
			print("⚠️ レート制限に達しました。しばらく待機してください。")
			time.sleep(60)
			break
		if response.status_code != 200:
			break
		for cm in response.json():
			fetchCmdata(cm["html_url"], output_root)
		url = response.links.get("next", {}).get("url")

# リポジトリURLからfix付きプルリク検索
def fetchPrs(repo_url, output_root):
	match = re.match(r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)", repo_url)
	if not match:
		return
	owner, repo = match.group("owner"), match.group("repo")
	url = f"https://api.github.com/repos/{owner}/{repo}/pulls?state=closed&per_page=100"

	while url:
		response = requests.get(url, headers=headers)
		time.sleep(1)  # レート制限対策
		if response.status_code in [403, 429]:
			print("⚠️ レート制限に達しました。しばらく待機してください。")
			time.sleep(60)
			break
		if response.status_code != 200:
			break
		for pr in response.json():
			if "fix" in pr["title"].lower():
				fetchCms(pr["html_url"], output_root)
		url = response.links.get("next", {}).get("url")

# API名でリポジトリ検索
def fetchRepos(api_name, output_root):
	url = f"https://api.github.com/search/repositories?q={api_name}+stars:>100+in:readme,description&sort=stars&order=desc&per_page=10"
	while url:
		response = requests.get(url, headers=headers)
		time.sleep(1)  # レート制限対策
		if response.status_code in [403, 429]:
			print("⚠️ レート制限に達しました。しばらく待機してください。")
			time.sleep(60)
			break
		if response.status_code != 200:
			break
		for repo in response.json().get("items", []):
			fetchPrs(repo["html_url"], output_root)
		url = response.links.get("next", {}).get("url")

# API名でコミットメッセージ検索
def search_commits_by_api_name(api_name, output_root):
	url = f"https://api.github.com/search/commits?q={api_name}+in:message&sort=author-date&order=desc&per_page=100"
	headers_preview = headers.copy()
	headers_preview["Accept"] = "application/vnd.github.cloak-preview"

	while url:
		response = requests.get(url, headers=headers_preview)
		time.sleep(1)  # レート制限対策
		if response.status_code in [403, 429]:
			print("⚠️ レート制限に達しました。しばらく待機してください。")
			time.sleep(60)
			break
		if response.status_code != 200:
			break
		for cm in response.json().get("items", []):
			fetchCmdata(cm["html_url"], output_root)
		url = response.links.get("next", {}).get("url")

# API名でPRタイトル検索
def search_pull_requests_by_api_name(api_name, output_root):
	url = f"https://api.github.com/search/issues?q={api_name}+in:title+type:pr&sort=created&order=desc&per_page=100"
	while url:
		response = requests.get(url, headers=headers)
		time.sleep(1)  # レート制限対策
		if response.status_code in [403, 429]:
			print("⚠️ レート制限に達しました。しばらく待機してください。")
			time.sleep(60)
			break
		if response.status_code != 200:
			break
		for pr in response.json().get("items", []):
			fetchCms(pr["html_url"], output_root)
		url = response.links.get("next", {}).get("url")

# スピナーを表示
def show_spinner(stop_event):
	spinner = itertools.cycle(["|", "/", "-", "\\"])
	while not stop_event.is_set():
		print(f"⌛ 処理中 {next(spinner)}", end="\r")
		time.sleep(0.2)


# === Main ===

if __name__ == "__main__":
	api_name = input("🔎 キーワードを入力してください: ")

	timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M")
	output_root = os.path.join("output", f"{api_name}({timestamp})")
	os.makedirs(output_root, exist_ok=True)

	stop_event = threading.Event()
	spinner_thread = threading.Thread(target=show_spinner, args=(stop_event,))
	spinner_thread.start()
	try:
		fetchRepos(api_name, output_root)
		search_commits_by_api_name(api_name, output_root)
		search_pull_requests_by_api_name(api_name, output_root)

		stop_event.set()
		spinner_thread.join()
		print(" " * 40, end="\r")

		finished_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
		patch_count = next(patch_counter)
		print(f"\n✅ 完了 - 保存したpatch数: {patch_count} - 完了時刻: {finished_time}")

		print("\n📊 言語ごとのパッチ数:")
		for lang, count in lang_patch_counter.items():
			print(f"  - {lang}: {count}")

		print(f"\n📁 保存ディレクトリ: {output_root}")

	except Exception as e:
		stop_event.set()
		spinner_thread.join()
		print(" " * 40, end="\r")
		print(f"\n❌ エラー: {e}")