import json
import os
import re
import time
from datetime import datetime
import requests

DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)

# 直接鎖定 Grand Archive 的 Category ID
CAT_ID = 74

HEADERS = {
    "User-Agent": "GAPriceTracker/1.0",
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
        print(f"   ⚠️ 無法下載產品或價格 (HTTP {p_res.status_code})")
        continue

      p_data = p_res.json().get("results", [])
      pr_data = pr_res.json().get("results", [])
    except Exception as e:
      print(f"   ⚠️ 抓取異常: {e}")
      continue

    # 建立商品價格對應字典：pid -> { subTypeName: { marketPrice, lowPrice, highPrice } }
    price_map = {}
    for p in pr_data:
      pid = p["productId"]
      sub = p.get("subTypeName") or "Normal"
      if pid not in price_map:
        price_map[pid] = {}
      price_map[pid][sub] = {
          "marketPrice": p.get("marketPrice") or 0.0,
          "lowPrice": p.get("lowPrice") or 0.0,
          "highPrice": p.get("highPrice") or 0.0,
      }

    # 整合單卡資訊
    for prod in p_data:
      pid = str(prod["productId"])
      card_name = prod.get("name", "Unknown")
      clean_name = prod.get("cleanName", card_name)
      ext_fields = {
          item.get("name"): item.get("value")
          for item in prod.get("extendedData", [])
      }
      rarity = ext_fields.get("Rarity", "Common")
      card_number = ext_fields.get("Number") or ext_fields.get("CardNumber") or f"#{pid}"

      # 按乾淨卡名分組（讓同名卡可以切換不同版本）
      if clean_name not in cards_by_name:
        cards_by_name[clean_name] = {
            "id": f"card-{len(cards_by_name)}",
            "name": clean_name,
            "classType": ext_fields.get("Class", "Grand Archive"),
            "rarity": rarity,
            "versions": [],
        }

      # 判定本身是否屬於特卡工藝 (CSR, CUR, CPR, CR)
      lower_name = card_name.lower()
      is_csr = "collector super rare" in rarity.lower() or "(csr)" in lower_name or bool(re.search(r'\bcsr\b', lower_name))
      is_cur = "collector ultra rare" in rarity.lower() or "(cur)" in lower_name or bool(re.search(r'\bcur\b', lower_name))
      is_cpr = "collector promo" in rarity.lower() or "(cpr)" in lower_name
      is_cr = ("collector's rare" in rarity.lower() or "collector rare" in rarity.lower() or "(cr)" in lower_name or bool(re.search(r'\bcr\b', lower_name))) and not is_csr

      special_finish = None
      if is_csr:
        special_finish = "CSR"
      elif is_cur:
        special_finish = "CUR"
      elif is_cpr:
        special_finish = "CPR"
      elif is_cr:
        special_finish = "CR"

      prod_prices = price_map.get(int(pid), {})

      # 準備版本列表（若同一卡牌同時有 Normal 與 Foil 價格，分別建立獨立版本，優先排列 Normal）
      versions_to_add = []
      if not prod_prices:
        finish = special_finish or ("Foil" if "foil" in lower_name else "Normal")
        versions_to_add.append({
            "versionId": f"{pid}-normal",
            "finish": finish,
            "subType": "Normal",
            "marketPrice": 0.0,
            "lowPrice": 0.0,
            "highPrice": 0.0,
        })
      else:
        # 1. 處理 Normal 版本
        if "Normal" in prod_prices:
          pr = prod_prices["Normal"]
          versions_to_add.append({
              "versionId": f"{pid}-normal",
              "finish": special_finish or "Normal",
              "subType": "Normal",
              "marketPrice": pr["marketPrice"],
              "lowPrice": pr["lowPrice"],
              "highPrice": pr["highPrice"],
          })
        # 2. 處理 Foil 版本
        if "Foil" in prod_prices:
          pr = prod_prices["Foil"]
          foil_finish = f"{special_finish} (Foil)" if special_finish else "Foil"
          versions_to_add.append({
              "versionId": f"{pid}-foil",
              "finish": foil_finish,
              "subType": "Foil",
              "marketPrice": pr["marketPrice"],
              "lowPrice": pr["lowPrice"],
              "highPrice": pr["highPrice"],
          })

      for ver_data in versions_to_add:
        v_key = ver_data["versionId"]
        market_p = ver_data["marketPrice"]

        # 維護歷史價格 (各版本獨立維護，只留最近 30 天)
        if v_key not in history_db:
          raw_pid = v_key.split("-")[0]
          # 若舊版本曾存有 raw_pid 紀錄且為 Normal，繼承舊紀錄
          if ver_data["subType"] == "Normal" and raw_pid in history_db:
            history_db[v_key] = list(history_db[raw_pid])
          else:
            history_db[v_key] = []

        if not history_db[v_key] or history_db[v_key][-1]["date"] != today_str:
          if market_p > 0:
            history_db[v_key].append({"date": today_str, "price": market_p})
            history_db[v_key] = history_db[v_key][-30:]

        # 計算昨日 vs 今日波動
        if len(history_db[v_key]) >= 2:
          yesterday_p = history_db[v_key][-2]["price"]
          if yesterday_p > 0 and market_p > 0:
            diff = market_p - yesterday_p
            pct = (diff / yesterday_p) * 100
            # 篩選條件：波動 >= 15% 且絕對差價 >= 1 美元
            if abs(pct) >= 15 and abs(diff) >= 1.0:
              daily_changes.append({
                  "name": f"{clean_name} [{ver_data['finish']}] ({grp_name})",
                  "oldPrice": f"${yesterday_p:.2f}",
                  "newPrice": f"${market_p:.2f}",
                  "change": f"{'+' if pct > 0 else ''}{pct:.1f}%",
                  "up": pct > 0,
                  "absDiff": abs(diff),
              })

        code_str = f"#{card_number}" if not str(card_number).startswith("#") else str(card_number)
        cards_by_name[clean_name]["versions"].append({
            "versionId": v_key,
            "edition": grp_name,
            "finish": ver_data["finish"],
            "subType": ver_data["subType"],
            "code": code_str,
            "image": prod.get("imageUrl") or "",
            "marketPrice": market_p,
            "lowPrice": ver_data["lowPrice"],
            "highPrice": ver_data["highPrice"],
            "history": history_db[v_key],
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