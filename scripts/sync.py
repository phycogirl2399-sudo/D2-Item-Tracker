#!/usr/bin/env python3
"""Per-user Destiny 2 vendor sync.

Every user links their OWN Bungie account in the app (refresh token stored in Firestore bungie/{uid}).
For each linked user this script reads that account's vendor stock, writes it to Firestore stock/{uid}
(private to that user) and sends that user's push alerts.

Env (GitHub Secrets): BUNGIE_API_KEY, BUNGIE_CLIENT_ID, BUNGIE_CLIENT_SECRET, FIREBASE_SERVICE_ACCOUNT
Public output (committed): data/possible.json, data/observed.json - static item catalog, no personal data.
Manifest cache lives in .cache/ (Actions cache).
"""
import gzip, json, os, sys, time
from pathlib import Path
import requests
import firebase_admin
from firebase_admin import credentials, firestore, messaging

ROOT = Path(__file__).resolve().parent.parent
DATA, STATE = ROOT / "data", ROOT / ".cache"
DATA.mkdir(exist_ok=True); STATE.mkdir(exist_ok=True)
BASE = "https://www.bungie.net"
API_KEY = os.environ["BUNGIE_API_KEY"]
db = None  # Firestore client, set in main()


def load(p, default):
    try:
        return json.loads(Path(p).read_text())
    except FileNotFoundError:
        return default


def save(p, obj):
    Path(p).write_text(json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False))


def get(path, token=None, **params):
    h = {"X-API-Key": API_KEY}
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = requests.get(BASE + path, headers=h, params=params, timeout=60)
    r.raise_for_status()
    j = r.json()
    if j.get("ErrorCode") != 1:
        raise RuntimeError(f"{path}: {j.get('ErrorStatus')} {j.get('Message')}")
    return j["Response"]


class RelinkNeeded(Exception):
    pass


def user_access_token(uid):
    """Refresh one user's Bungie token (Bungie rotates the refresh token every time, so store the new one)."""
    ref = db.collection("bungie").document(uid)
    snap = ref.get()
    if not snap.exists:
        raise RelinkNeeded()
    r = requests.post(BASE + "/Platform/App/OAuth/token/", headers={"X-API-Key": API_KEY}, data={
        "grant_type": "refresh_token", "refresh_token": snap.to_dict()["refresh_token"],
        "client_id": os.environ["BUNGIE_CLIENT_ID"], "client_secret": os.environ["BUNGIE_CLIENT_SECRET"]}, timeout=60)
    if r.status_code in (400, 401):      # revoked / expired (~90 days unused): user must reconnect in the app
        raise RelinkNeeded()
    r.raise_for_status()
    j = r.json()
    ref.set({"refresh_token": j["refresh_token"], "updated": int(time.time())}, merge=True)
    return j["access_token"]


def ensure_manifest():
    """Slim item + vendor name tables, rebuilt only when Bungie's manifest version changes."""
    m = get("/Platform/Destiny2/Manifest/")
    meta = load(STATE / "manifest_version.json", {})
    ver = f'{m["version"]}|{SCHEMA}'
    if meta.get("version") == ver and (STATE / "items.json").exists():
        return load(STATE / "items.json", {}), load(STATE / "vendor_names.json", {})
    paths = m["jsonWorldComponentContentPaths"]["en"]
    raw = requests.get(BASE + paths["DestinyInventoryItemDefinition"], timeout=300).json()
    items = {}
    for h, d in raw.items():
        dp = d.get("displayProperties", {})
        if not dp.get("name"):
            continue
        items[h] = {"n": dp["name"], "i": dp.get("icon", ""), "t": d.get("itemTypeDisplayName", ""),
                    "r": d.get("inventory", {}).get("tierTypeName", ""), "c": d.get("itemType", 0),
                    "a": d.get("equippingBlock", {}).get("ammoType", 0), "k": d.get("classType", 3),
                    "d": d.get("defaultDamageType", 0), "b": d.get("inventory", {}).get("bucketTypeHash", 0),
                    "e": 1 if 34 in d.get("itemCategoryHashes", []) or "Engram" in d.get("itemTypeDisplayName", "") else 0}
    vraw = requests.get(BASE + paths["DestinyVendorDefinition"], timeout=300).json()
    vnames = {h: d.get("displayProperties", {}).get("name", "") for h, d in vraw.items()}
    # Every item any vendor can possibly sell (static vendor definitions)
    possible = {}
    for vh, vd in vraw.items():
        vn = vd.get("displayProperties", {}).get("name")
        if not vn or vd.get("redacted"):
            continue
        for e in vd.get("itemList", []):
            ih = str(e.get("itemHash", 0))
            it = items.get(ih)
            if not it or not it["i"] or it["e"]:   # no icon = not a real item; skip engrams
                continue
            if vn not in possible.setdefault(ih, []):
                possible[ih].append(vn)
    save(STATE / "possible_base.json", possible)   # hash -> [vendor names] from Bungie's static vendor definitions
    save(STATE / "items.json", items); save(STATE / "vendor_names.json", vnames)
    save(STATE / "manifest_version.json", {"version": ver})
    return items, vnames


SCHEMA = 3
SLOT = {1498876634: "Kinetic", 2465295065: "Energy", 953998645: "Power", 3448274439: "Helmet",
        3551918588: "Gauntlets", 14239492: "Chest", 20886954: "Legs", 1585787867: "Class Item"}


def to_entry(ih, it):
    """Slim, app-ready item record. group: primary/special/heavy/exotic_gun/armor/exotic_armor/misc."""
    exotic = it["r"] == "Exotic"
    if it["c"] == 3:
        group = "exotic_gun" if exotic else {1: "primary", 2: "special", 3: "heavy"}.get(it["a"], "primary")
    elif it["c"] == 2:
        group = "exotic_armor" if exotic else "armor"
    else:
        group = "misc"
    return {"hash": ih, "name": it["n"], "icon": it["i"], "type": it["t"], "tier": it["r"], "group": group,
            "ammo": it["a"], "klass": it["k"], "dmg": it["d"], "slot": SLOT.get(it["b"], "")}


def fetch_sales(access):
    ms = get("/Platform/User/GetMembershipsForCurrentUser/", access)
    pid = ms.get("primaryMembershipId")
    mem = next((x for x in ms["destinyMemberships"] if x["membershipId"] == pid), ms["destinyMemberships"][0])
    t, mid = mem["membershipType"], mem["membershipId"]
    chars = get(f"/Platform/Destiny2/{t}/Profile/{mid}/", access, components=200)["characters"]["data"]
    cid = next(iter(chars))
    # 400 = vendors, 402 = sales (costs included). Vendor stock can differ per class, so union all characters.
    sales = {}
    for cid in chars:
        r = get(f"/Platform/Destiny2/{t}/Profile/{mid}/Character/{cid}/Vendors/", access, components="400,402")
        for vh, v in (r.get("sales", {}).get("data") or {}).items():
            for idx, s in v["saleItems"].items():
                if s["itemHash"] == 0:
                    continue
                key = f'{vh}:{s["itemHash"]}'
                sales.setdefault(key, {"vendor": vh, "item": str(s["itemHash"]),
                                       "costs": [{"item": str(c["itemHash"]), "q": c["quantity"]} for c in s.get("costs", [])],
                                       "purchasable": s.get("failureIndexes", []) == [] and s.get("saleStatus", 0) == 0})
    return list(sales.values())


def send_push(uid, tokens, title, body):
    """Send to every device of one user; prune dead tokens."""
    for tk in tokens:
        try:
            messaging.send(messaging.Message(token=tk, notification=messaging.Notification(title=title, body=body),
                                             android=messaging.AndroidConfig(priority="high")))
        except (messaging.UnregisteredError, messaging.SenderIdMismatchError):
            db.collection("users").document(uid).update({"tokens": firestore.ArrayRemove([tk])})
        except Exception as e:  # keep going for other devices/users
            print("FCM error for", uid, type(e).__name__)


def build_vendors(sales, items, vnames):
    vendors = {}
    for s_ in sales:
        it = items.get(s_["item"])
        if not it or not vnames.get(s_["vendor"]):
            continue
        cost = [{"name": items.get(c["item"], {}).get("n", c["item"]), "icon": items.get(c["item"], {}).get("i", ""),
                 "q": c["q"]} for c in s_["costs"]]
        vendors.setdefault(s_["vendor"], {"hash": s_["vendor"], "name": vnames[s_["vendor"]], "items": []})
        vendors[s_["vendor"]]["items"].append({**to_entry(s_["item"], it), "cost": cost, "buyable": s_["purchasable"]})
    return sorted(vendors.values(), key=lambda v: v["name"])


def alert_user(uid, d, vendors):
    """Push once when a watched item becomes buyable for this user; re-arm after it leaves the vendor."""
    watch, tokens = set(d.get("watch", [])), d.get("tokens", [])
    nref = db.collection("notified").document(uid)
    prev = set((nref.get().to_dict() or {}).get("keys", []))
    keys, sent = set(), 0
    for v in vendors:
        for it in v["items"]:
            if it["hash"] in watch and it["buyable"]:
                k = f'{v["hash"]}:{it["hash"]}'
                keys.add(k)
                if k not in prev:
                    cost = ", ".join(f'{c["name"]} x{c["q"]}' for c in it["cost"]) or "free"
                    send_push(uid, tokens, f'{it["name"]} is for sale', f'{v["name"]} - {cost}')
                    sent += 1
    if keys != prev:
        nref.set({"keys": sorted(keys)})
    return sent


def main():
    global db
    firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])))
    db = firestore.client()
    items, vnames = ensure_manifest()
    now = int(time.time())
    observed = load(DATA / "observed.json", {})
    users = ok = alerts = 0

    for doc in db.collection("users").stream():
        d = doc.to_dict() or {}
        if not d.get("bungieLinked"):
            continue
        users += 1
        uref = db.collection("users").document(doc.id)
        try:
            vendors = build_vendors(fetch_sales(user_access_token(doc.id)), items, vnames)
        except RelinkNeeded:
            uref.set({"bungieStatus": "relink"}, merge=True)
            continue
        except Exception as e:      # one user's failure must not stop everyone else
            print("sync failed for a user:", type(e).__name__, str(e)[:200])
            continue
        blob = gzip.compress(json.dumps({"updated": now, "vendors": vendors}, separators=(",", ":")).encode())
        db.collection("stock").document(doc.id).set({"updated": now, "data": blob})
        if d.get("bungieStatus") != "ok":
            uref.set({"bungieStatus": "ok"}, merge=True)
        for v in vendors:                                   # feed the shared "everything a vendor can sell" list
            for it in v["items"]:
                if not items.get(it["hash"], {}).get("e") and v["name"] not in observed.setdefault(it["hash"], []):
                    observed[it["hash"]].append(v["name"])
        alerts += alert_user(doc.id, d, vendors)
        ok += 1

    save(DATA / "observed.json", observed)
    # Search list = Bungie's static vendor definitions + everything any user's account has been seen buying
    merged = {h: list(vs) for h, vs in load(STATE / "possible_base.json", {}).items()}
    for h, vs in observed.items():
        for vn in vs:
            if vn not in merged.setdefault(h, []):
                merged[h].append(vn)
    save(DATA / "possible.json", {"updated": now, "items": [
        {**to_entry(h, items[h]), "vendors": sorted(vs)} for h, vs in merged.items() if h in items]})
    print(f"{ok}/{users} linked users synced, {alerts} alerts")


if __name__ == "__main__":
    main()
