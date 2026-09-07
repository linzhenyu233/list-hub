# -*- coding: utf-8 -*-
"""Replace saved WeChat category chains with the latest cats_v2 chains."""

import json
import sqlite3
import urllib.parse
import urllib.request

from bulk_api import DB_FILE, WECHAT_API_BASE, now


def latest_chain(saved, cache):
    chain = saved.get("chain") or []
    if not chain:
        return None
    leaf = chain[-1]
    leaf_id = str(leaf.get("cat_id") or leaf.get("id") or "")
    leaf_name = str(leaf.get("name") or "").strip()
    cache_key = (leaf_id, leaf_name)
    if cache_key in cache:
        return cache[cache_key]
    url = f"{WECHAT_API_BASE}/categories?keyword={urllib.parse.quote(leaf_name)}"
    with urllib.request.urlopen(url, timeout=180) as response:
        candidates = json.loads(response.read().decode("utf-8")).get("results", [])
    matches = [item for item in candidates if item.get("leaf") and str(item.get("cat_id")) == leaf_id]
    if len(matches) != 1:
        names = [str(node.get("name") or "").strip() for node in chain]
        matches = [item for item in candidates if item.get("leaf") and
                   [str(node.get("name") or "").strip() for node in item.get("chain", [])] == names]
    if len(matches) != 1:
        cache[cache_key] = None
        return None
    match = matches[0]
    refreshed = {**saved, "category": match.get("path"), "chain": match.get("chain") or []}
    cache[cache_key] = refreshed
    return refreshed


def main():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    aliases_updated = batches_updated = skipped = 0
    cache = {}
    for row in connection.execute("SELECT id, wechat_json FROM category_aliases WHERE wechat_json IS NOT NULL"):
        saved = json.loads(row["wechat_json"])
        refreshed = latest_chain(saved, cache)
        if not refreshed:
            skipped += 1
            continue
        if refreshed != saved:
            connection.execute("UPDATE category_aliases SET wechat_json=?, updated_at=? WHERE id=?",
                               (json.dumps(refreshed, ensure_ascii=False), now(), row["id"]))
            aliases_updated += 1
    for row in connection.execute("SELECT id, mappings_json FROM batches"):
        mappings = json.loads(row["mappings_json"] or "{}")
        changed = False
        for product_mapping in (mappings.get("products") or {}).values():
            chain = product_mapping.get("wechat_category_chain") or []
            if not chain:
                continue
            refreshed = latest_chain({"chain": chain, "category": product_mapping.get("wechat_category", "")}, cache)
            if refreshed and refreshed["chain"] != chain:
                product_mapping["wechat_category_chain"] = refreshed["chain"]
                product_mapping["wechat_category"] = refreshed["category"]
                changed = True
        if changed:
            connection.execute("UPDATE batches SET mappings_json=? WHERE id=?",
                               (json.dumps(mappings, ensure_ascii=False), row["id"]))
            batches_updated += 1
    connection.commit()
    connection.close()
    print(json.dumps({"aliases_updated": aliases_updated, "batches_updated": batches_updated,
                      "skipped": skipped}, ensure_ascii=False))


if __name__ == "__main__":
    main()
