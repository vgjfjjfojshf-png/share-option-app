"""
End-to-end test for the Share Option backend.

1) Start the server with test settings (fresh DB, the schema changed: delete the old file):
     rm -f share_option.db
     ADMIN_SECRET=test-secret AUTO_CONFIRM_PAYMENTS=true RATE_LIMIT_DISABLED=true uvicorn main:app --port 8000
2) Run:  ADMIN_SECRET=test-secret python e2e_test.py        (needs: pip install httpx)

Covers: broker accounts, demo-vs-real commission, admin totals, parser -> fan-out -> execute -> settle -> fee accrual -> $50 suspension ->
        blocked delivery -> payment -> reactivation, loss credit, and the Compounding ladder.
"""
import asyncio
import os
import secrets
import sys

import httpx

BASE = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
ADMIN = {"X-Admin-Secret": os.environ["ADMIN_SECRET"]}
_msg = 0
failures = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global failures
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))
    failures += not cond


async def register(c: httpx.AsyncClient, name: str, balance: float = 1000, account: bool = True,
                   account_type: str = "REAL") -> dict:
    """Creates an account (email + password), sets capital, and by default links + admin-verifies a broker account."""
    n = secrets.token_hex(4)
    r = await c.post("/api/auth/register", json={"accept_terms": True, "name": name, "email": f"{n}@test.dev", "password": "Passw0rd!" + n})
    r.raise_for_status()
    h = {"Authorization": f"Bearer {r.cookies.get('so_session')}"}
    if balance:
        (await c.post("/api/user/balance", headers=h, json={"balance": balance})).raise_for_status()
    if account:
        a = await c.post("/api/user/broker-account", headers=h, json={
            "broker": "POCKET_OPTION", "broker_uid": "T" + secrets.token_hex(5), "account_type": account_type, "plan": "PASSIVE"})
        a.raise_for_status()
        (await c.post(f"/api/admin/broker-accounts/{a.json()['id']}/verify", headers=ADMIN, json={"status": "VERIFIED"})).raise_for_status()
    return h


async def status(c, h) -> dict:
    return (await c.get("/api/user/status", headers=h)).json()


async def new_signal(c, text="EUR/USD 5 MIN CALL NOW") -> dict:
    global _msg
    _msg += 1
    r = await c.post("/api/signals/parse", headers=ADMIN, json={"text": text, "channel_id": "test", "message_id": _msg})
    r.raise_for_status()
    return r.json()


async def trade(c, h, outcome: str) -> httpx.Response:
    """Signal -> execute -> settle. Returns the execute response."""
    sig = await new_signal(c)
    ex = await c.post(f"/api/signals/{sig['signal']['id']}/execute", headers=h)
    if ex.status_code == 200:
        await c.post(f"/api/admin/signals/{sig['signal']['id']}/result", headers=ADMIN,
                     json={"outcome": outcome, "payout_pct": 85})
    return ex


async def pay_all(c, h) -> None:
    s = await status(c, h)
    if s["unpaid_fees"] > 0:
        await c.post("/api/user/settle-fees", headers=h,
                     json={"amount": s["unpaid_fees"], "txid": secrets.token_hex(16), "network": "TRC20"})


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=10) as c:
        print("Health / parser")
        check("health ok", (await c.get("/api/health")).json()["status"] == "ok")
        ign = await new_signal(c, "Good morning team!")
        check("non-signal is ignored", ign["status"] == "IGNORED", str(ign))
        check("requires admin secret", (await c.post("/api/signals/parse", json={"text": "x"})).status_code == 422
              or (await c.post("/api/signals/parse", json={"text": "x"}, headers={"X-Admin-Secret": "bad"})).status_code == 403)

        print("Accounts & sessions (email / phone / password)")
        n = secrets.token_hex(4)
        email, pw = f"auth{n}@test.dev", "Sup3rSecret!"
        r = await c.post("/api/auth/register", json={"accept_terms": True, "name": "Auth Tester", "email": email, "password": pw})
        tok = r.cookies.get("so_session")
        check("register by email sets a session cookie", r.status_code == 200 and bool(tok), r.text)
        hh = {"Authorization": f"Bearer {tok}"}
        check("session grants access", (await c.get("/api/user/status", headers=hh)).status_code == 200)
        check("duplicate email rejected (case-insensitive)", (await c.post("/api/auth/register", json={"accept_terms": True, "name": "X Y", "email": email.upper(), "password": pw})).status_code == 409)
        check("weak password rejected", (await c.post("/api/auth/register", json={"accept_terms": True, "name": "X Y", "email": f"w{n}@test.dev", "password": "short"})).status_code == 422)
        check("email or phone required", (await c.post("/api/auth/register", json={"accept_terms": True, "name": "X Y", "password": pw})).status_code == 422)
        phone = "+2126" + f"{int(n, 16) % 10 ** 8:08d}"
        check("register by phone", (await c.post("/api/auth/register", json={"accept_terms": True, "name": "Phone Tester", "phone": phone, "password": pw})).status_code == 200)
        check("wrong password -> 401", (await c.post("/api/auth/login", json={"identifier": email, "password": "nope-nope"})).status_code == 401)
        check("login by email", (await c.post("/api/auth/login", json={"identifier": email, "password": pw})).status_code == 200)
        check("login by phone", (await c.post("/api/auth/login", json={"identifier": phone, "password": pw})).status_code == 200)
        c.cookies.clear()
        check("old API-key header no longer works", (await c.get("/api/user/status", headers={"X-API-Key": tok})).status_code == 401)
        await c.post("/api/auth/logout", headers={"Cookie": f"so_session={tok}"})
        check("logout invalidates the session", (await c.get("/api/user/status", headers=hh)).status_code == 401)

        print("Passive plan: fees, suspension, payment")
        h = await register(c, "Passive Tester")
        s = await status(c, h)
        check("starts ACTIVE with 5% stake", s["account_status"] == "ACTIVE" and s["next_stake"] == 50.0, str(s))
        wins = 0
        while (await status(c, h))["account_status"] == "ACTIVE" and wins < 12:
            ex = await trade(c, h, "WIN")
            check(f"win #{wins + 1} executed", ex.status_code == 200, ex.text)
            wins += 1
        s = await status(c, h)
        check("suspended once unpaid fees >= $50", s["account_status"] == "SUSPENDED" and s["unpaid_fees"] >= 50, str(s))
        sig = await new_signal(c)
        check("suspended user is BLOCKED in fan-out", sig["blocked"] >= 1, str(sig["blocked"]))
        check("suspended user gets no signals", (await c.get("/api/signals", headers=h)).json()["blocked"] is True)
        ex = await c.post(f"/api/signals/{sig['signal']['id']}/execute", headers=h)
        check("suspended execute -> 403", ex.status_code == 403, ex.text)
        dup = await c.post("/api/user/settle-fees", headers=h, json={"amount": 1, "txid": "x" * 20})
        dup2 = await c.post("/api/user/settle-fees", headers=h, json={"amount": 1, "txid": "x" * 20})
        check("duplicate txid rejected", dup2.status_code == 409, dup2.text)
        await pay_all(c, h)
        s = await status(c, h)
        check("paying fees reactivates account", s["account_status"] == "ACTIVE" and s["unpaid_fees"] == 0, str(s))

        print("Loss credit")
        await trade(c, h, "WIN")           # creates a fee
        before = await status(c, h)
        await trade(c, h, "LOSS")          # 20% of the loss is credited against the fee
        after = await status(c, h)
        check("loss reduces unpaid fees", after["unpaid_fees"] < before["unpaid_fees"], f"{before['unpaid_fees']} -> {after['unpaid_fees']}")

        print("Compounding ladder 10 -> 20 -> 30 -> 10, loss resets to 10")
        h2 = await register(c, "Compounding Tester")
        r = await c.post("/api/user/plan", headers=h2, json={"plan": "COMPOUNDING"})
        check("switch to COMPOUNDING", r.status_code == 200, r.text)
        seen = []
        for _ in range(4):
            seen.append((await status(c, h2))["next_stake_pct"])
            await trade(c, h2, "WIN")
            await pay_all(c, h2)           # keep the account under the $50 limit
        check("ladder follows 10,20,30,10", seen == [10, 20, 30, 10], str(seen))
        await trade(c, h2, "WIN")          # 10% -> 20%
        await pay_all(c, h2)
        await trade(c, h2, "LOSS")
        check("a loss resets stake to 10%", (await status(c, h2))["next_stake_pct"] == 10, str(await status(c, h2)))

        print("Automatic result settlement from channel messages")
        hr = await register(c, "Result Tester")
        sig = await new_signal(c, "AUD/CAD 1 MIN PUT NOW")
        check("signal for auto-settle created", sig["status"] == "OK", str(sig))
        await c.post(f"/api/signals/{sig['signal']['id']}/execute", headers=hr)
        res = await new_signal(c, "AUD/CAD ✅ WIN")
        check("result message settles the open trade", res["status"] == "RESULT" and res["settled"] >= 1 and res["outcome"] == "WIN", str(res))
        sr = await status(c, hr)
        check("auto-settled win accrued commission", sr["unpaid_fees"] > 0 and sr["balance"] > 1000, str(sr))
        again = await new_signal(c, "AUD/CAD ✅ WIN")
        check("repeat result does not double-settle", again["status"] == "IGNORED", str(again))
        check("plain chat with WIN word is not a result", (await new_signal(c, "Good morning, let’s win today!"))["status"] == "IGNORED")

        print("Admin site settings & terms")
        check("terms are required to register", (await c.post("/api/auth/register", json={"name": "No Terms", "email": f"nt{secrets.token_hex(3)}@test.dev", "password": "Passw0rd!x"})).status_code == 422)
        before = (await c.get("/api/admin/settings", headers=ADMIN)).json()
        check("settings expose a readiness checklist", len(before["checks"]) >= 6 and "link_quotex" in before["values"], str(before)[:200])
        check("non-https link rejected", (await c.post("/api/admin/settings", headers=ADMIN, json={"link_quotex": "http://x.test/ref"})).status_code == 422)
        check("invalid USDT address rejected", (await c.post("/api/admin/settings", headers=ADMIN, json={"usdt_address": "T-not-an-address"})).status_code == 422)
        check("unknown setting rejected", (await c.post("/api/admin/settings", headers=ADMIN, json={"nope": "x"})).status_code == 422)
        r = await c.post("/api/admin/settings", headers=ADMIN, json={"link_quotex": "https://example.com/ref-test"})
        check("link saved and live immediately", r.status_code == 200 and (await c.get("/api/config/brokers")).json()["QUOTEX"] == "https://example.com/ref-test", r.text[:200])
        await c.post("/api/admin/settings", headers=ADMIN, json={"link_quotex": before["values"]["link_quotex"]})  # restore

        print("Passive stake choice")
        r = await c.post("/api/user/plan", headers=h, json={"plan": "PASSIVE", "stake_pct": 10})
        check("stake_pct 10 accepted", r.status_code == 200 and r.json()["next_stake_pct"] == 10.0, r.text)
        r = await c.post("/api/user/plan", headers=h, json={"plan": "PASSIVE", "stake_pct": 25})
        check("stake_pct 25 rejected", r.status_code == 422)

        print("Balance sync drives Compounding entry")
        r = await c.post("/api/user/balance", headers=h2, json={"balance": 2000})
        check("compounding entry = 10% of synced balance", r.status_code == 200 and r.json()["next_stake"] == 200.0, r.text)
        check("trade-links exposed", "QUOTEX" in (await c.get("/api/config/trade-links")).json())

        print("Broker accounts, verification gate & commission tracking")
        acct = {"broker": "POCKET_OPTION", "broker_uid": "PO" + secrets.token_hex(4), "account_type": "REAL", "plan": "PASSIVE", "via_link": True}
        h5 = await register(c, "Unverified Tester", account=False)
        r = await c.post("/api/user/broker-account", headers=h5, json=acct)
        check("broker account registered as PENDING", r.status_code == 200 and r.json()["status"] == "PENDING", r.text)
        acct_id = r.json()["id"]
        check("same broker UID cannot be claimed twice", (await c.post("/api/user/broker-account", headers=h, json=acct)).status_code == 409)
        check("invalid UID rejected", (await c.post("/api/user/broker-account", headers=h5, json={**acct, "broker_uid": "bad uid!"})).status_code == 422)
        check("power-share broker link accepted", (await c.post("/api/user/broker-account", headers=h5, json={**acct, "plan": "POWER_POOL"})).status_code == 200)
        s5 = await status(c, h5)
        check("unverified account is gated", s5["can_trade"] is False and s5["block_reason"] == "BROKER_NOT_VERIFIED", str(s5))
        sig = await new_signal(c)
        check("unverified user is BLOCKED in fan-out", any(e.get("reason") == "BROKER_NOT_VERIFIED" for e in sig["executions"]))
        check("unverified signals list is locked", (await c.get("/api/signals", headers=h5)).json().get("reason") == "BROKER_NOT_VERIFIED")
        check("unverified execute -> 403", (await c.post(f"/api/signals/{sig['signal']['id']}/execute", headers=h5)).status_code == 403)
        await c.post(f"/api/admin/broker-accounts/{acct_id}/verify", headers=ADMIN, json={"status": "VERIFIED"})
        check("verification unlocks trading", (await status(c, h5))["can_trade"] is True)
        check("broker links exposed", "POCKET_OPTION" in (await c.get("/api/config/brokers")).json())

        h4 = await register(c, "Demo Tester", account_type="DEMO")
        await trade(c, h4, "WIN")
        s4 = await status(c, h4)
        check("demo account wins carry no commission", s4["unpaid_fees"] == 0 and s4["balance"] > 1000, str(s4))
        h3 = await register(c, "Broker Tester")
        await trade(c, h3, "WIN")
        check("real account wins accrue commission", (await status(c, h3))["unpaid_fees"] > 0)

        d = (await c.get("/api/admin/clients", headers=ADMIN)).json()
        row = next(x for x in d["rows"] if x["name"] == "Broker Tester")
        check("admin sees account + commission", row["broker_accounts"][0]["account_type"] == "REAL" and row["commission"]["accrued"] > 0, str(row))
        check("admin totals present", d["totals"]["accrued"] > 0 and "REAL" in d["by_account_type"] and "DEMO" in d["by_account_type"], str(d["by_account_type"]))

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{failures} CHECK(S) FAILED"))
    sys.exit(1 if failures else 0)


asyncio.run(main())
