from datetime import datetime
import json
import os
import time
import requests

DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)

# 直接鎖定 Grand Archive 的 Category ID
CAT_ID = 74

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def run_pipeline():
  today_str = datetime.now().strftime("%m/%d")

  # 1. 抓取 Grand Archive 所有系列 (Groups)
  print(f"正在取得 Grand Archive (Category {CAT_ID}) 的系列清單...")
  groups_url = f"https://tcgcsv.com/tcgplayer/{CAT_ID}/groups"

  res = requests.get(groups_url, headers=HEADERS, timeout=15)
  if res.status_code != 200:
    print(f"❌ 取得系列失敗: HTTP {res.status_code}")
    return

  groups = res.json().get("results", [])
  print(f"✅ 共找到 {len(groups)} 個系列。\n")

  # 讀取現有歷史價格走勢紀錄
  history_file = os.path.join(DATA_DIR, "history.json")
  history_db = {}
  if os.path.exists(history_file):
    try:
      with open(history_file, "r", encoding="utf-8") as f:
        history_db = json.load(f)
    except Exception:
      history_db = {}

  cards_by_name = {}
  daily_changes = []

  # 2. 遍歷系列拉取單卡與價格
  for idx, grp in enumerate(groups, 1):
    grp_id = grp["groupId"]
    grp_name = grp["name"]
    print(f"[{idx}/{len(groups)}] 正在抓取: {grp_name} (ID: {grp_id})")

    prod_url = f"https://tcgcsv.com/tcgplayer/{CAT_ID}/{grp_id}/products"
    price_url = f"https://tcgcsv.com/tcgplayer/{CAT_ID}/{grp_id}/prices"

    try:
      p_res = requests.get(prod_url, headers=HEADERS, timeout=20)
      pr_res = requests.get(price_url, headers=HEADERS, timeout=20)

      if p_res.status_code != 200 or pr_res.status_code != 200:
        print(f"   ⚠️️ 無法下載產品或價格 (HTTP {p_res.status_code})")
        continue

      p_data = p_res.json().get("results", [])
      pr_data = pr_res.json().get("results", [])
    except Exception as e:
      print(f"   ⚠️ 抓取異常: {e}")
      continue

    # 建立商品價格對應字典
    price_map = {}
    for p in pr_data:
      price_map[p["productId"]] = {
          "marketPrice": p.get("marketPrice") or 0.0,
          "lowPrice": p.get("lowPrice") or 0.0,
          "highPrice": p.get("highPrice") or 0.0,
      }

    # 整合單卡資訊
    for prod in p_data:
      pid = str(prod["productId"])
      card_name = prod.get("name", "Unknown")
      clean_name = prod.get("cleanName", card_name)
      prices = price_map.get(
          int(pid), {"marketPrice": 0.0, "lowPrice": 0.0, "highPrice": 0.0}
      )
      market_p = prices["marketPrice"]

      # 維護歷史價格 (只留最近 30 天)
      if pid not in history_db:
        history_db[pid] = []

      # 避免同天多次執行重複寫入
      if not history_db[pid] or history_db[pid][-1]["date"] != today_str:
        if market_p > 0:
          history_db[pid].append({"date": today_str, "price": market_p})
          history_db[pid] = history_db[pid][-30:]

      # 計算昨日 vs 今日波動
      if len(history_db[pid]) >= 2:
        yesterday_p = history_db[pid][-2]["price"]
        if yesterday_p > 0 and market_p > 0:
          diff = market_p - yesterday_p
          pct = (diff / yesterday_p) * 100
          # 篩選條件：波動 >= 15% 且絕對差價 >= 1 美元
          if abs(pct) >= 15 and abs(diff) >= 1.0:
            daily_changes.append({
                "name": f"{clean_name} ({grp_name})",
                "oldPrice": f"${yesterday_p:.2f}",
                "newPrice": f"${market_p:.2f}",
                "change": f"{'+' if pct > 0 else ''}{pct:.1f}%",
                "up": pct > 0,
                "absDiff": abs(diff),
            })

      # 按乾淨卡名分組（讓同名卡可以切換不同版本）
      if clean_name not in cards_by_name:
        ext_fields = {
            item.get("name"): item.get("value")
            for item in prod.get("extendedData", [])
        }
        cards_by_name[clean_name] = {
            "id": f"card-{len(cards_by_name)}",
            "name": clean_name,
            "classType": ext_fields.get("Class", "Grand Archive"),
            "rarity": ext_fields.get("Rarity", "Common"),
            "versions": [],
        }

      # 判定工藝/特殊版本
      finish = "Regular"
      lower_name = card_name.lower()
      if "collector's super rare" in lower_name or "csr" in lower_name:
        finish = "CSR"
      elif "collector's rare" in lower_name or "cr" in lower_name:
        finish = "CR"
      elif "foil" in lower_name:
        finish = "Foil"

      cards_by_name[clean_name]["versions"].append({
          "versionId": pid,
          "edition": grp_name,
          "finish": finish,
          "code": prod.get("extendedData", [{}])[0].get("value", f"#{pid}")
          if prod.get("extendedData")
          else f"#{pid}",
          "image": prod.get("imageUrl") or "",
          "marketPrice": market_p,
          "lowPrice": prices["lowPrice"],
          "highPrice": prices["highPrice"],
          "history": history_db[pid],
      })

    time.sleep(0.3)  # 禮貌延遲

  # 整理異動排行
  daily_changes.sort(key=lambda x: x["absDiff"], reverse=True)

  # 儲存 JSON 檔案
  final_card_list = list(cards_by_name.values())
  with open(
      os.path.join(DATA_DIR, "cards_db.json"), "w", encoding="utf-8"
  ) as f:
    json.dump(final_card_list, f, ensure_ascii=False, indent=2)

  with open(history_file, "w", encoding="utf-8") as f:
    json.dump(history_db, f, ensure_ascii=False, indent=2)

  with open(os.path.join(DATA_DIR, "volatile.json"), "w", encoding="utf-8") as f:
    json.dump(daily_changes[:15], f, ensure_ascii=False, indent=2)

  print(
      f"\n🎉 抓取完成！共整合 {len(final_card_list)} 張卡牌，今日大幅波動: {len(daily_changes)} 張"
  )


if __name__ == "__main__":
  run_pipeline()