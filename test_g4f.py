"""
test_g4f.py — запусти это на своём ноутбуке чтобы найти рабочие провайдеры.
python test_g4f.py
"""
import asyncio
import g4f
import g4f.Provider as P

TEST_MSG = [{"role": "user", "content": "Привет, как дела? Ответь одним предложением."}]
TIMEOUT = 10

PROVIDERS = [
    ("DuckDuckGo",   "gpt-4o-mini"),
    ("You",          "gpt-4o"),
    ("Blackbox",     "gpt-4o"),
    ("Yqcloud",      "gpt-4"),
    ("OperaAria",    "gpt-4o"),
    ("AnyProvider",  "gpt-4o-mini"),
    ("DeepInfra",    "gpt-4o-mini"),
    ("Liaobots",     "gpt-4o"),
    ("FreeChatgpt",  "gpt-4o-mini"),
    ("ChatGptt",     "gpt-4o"),
    ("Pizzagpt",     "gpt-4"),
    ("GizAI",        "gpt-4o-mini"),
    ("OIVSCode",     "gpt-4o-mini"),
    ("PollinationsAI", "gpt-4o"),
]

async def test(name, model):
    obj = getattr(P, name, None)
    if not obj:
        return name, "❌ не найден в g4f"
    try:
        resp = await asyncio.wait_for(
            g4f.ChatCompletion.create_async(
                model=model, messages=TEST_MSG, provider=obj
            ),
            timeout=TIMEOUT,
        )
        if resp and len(resp.strip()) > 5:
            return name, f"✅ {resp.strip()[:80]}"
        return name, "⚠️ пустой ответ"
    except asyncio.TimeoutError:
        return name, f"⏱ таймаут {TIMEOUT}с"
    except Exception as e:
        return name, f"❌ {type(e).__name__}: {str(e)[:60]}"

async def main():
    print(f"g4f версия: {g4f.version.utils.current_version}\n")
    tasks = [test(n, m) for n, m in PROVIDERS]
    results = await asyncio.gather(*tasks)
    print("=" * 60)
    working = []
    for name, result in results:
        print(f"{name:20} {result}")
        if result.startswith("✅"):
            working.append(name)
    print("=" * 60)
    print(f"\nРабочие провайдеры: {working}")
    print("\nВставь их в _PROVIDER_CONFIG в ai_service.py")

asyncio.run(main())