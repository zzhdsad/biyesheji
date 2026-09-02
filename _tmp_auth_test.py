import httpx, json, asyncio

async def test():
    async with httpx.AsyncClient(base_url="http://localhost:8000") as c:
        # 1. register
        r = await c.post("/api/v1/auth/register", json={
            "email": "curl_test@test.com",
            "username": "curluser",
            "password": "secret123",
        })
        print(f"[register] status={r.status_code} body={r.text[:300]}")
        
        # 2. login
        r = await c.post("/api/v1/auth/login", json={
            "username_or_email": "curluser",
            "password": "secret123",
        })
        print(f"[login] status={r.status_code} body={r.text[:300]}")
        
        # 3. protected (no token)
        r = await c.get("/api/v1/chat/conversations")
        print(f"[protected no token] status={r.status_code} body={r.text[:200]}")
        
        # 4. protected (with token)
        if r.status_code != 200:  # actually login response
            login_body = json.loads(r.text) if r.text else {}
            token = login_body.get("access_token", "")
            if token:
                r = await c.get("/api/v1/chat/conversations", headers={"Authorization": f"Bearer {token}"})
                print(f"[protected with token] status={r.status_code}")
                r = await c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
                print(f"[me] status={r.status_code} body={r.text[:200]}")

asyncio.run(test())
