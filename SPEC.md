# erranderpy — Proje Tanımı (v0.1)

> Telegram'dan yazdığım doğal Türkçe mesajları anlayıp ilgili küçük agent'a yönlendiren,
> yeni agent eklemenin bir klasör eklemek kadar kolay olduğu, hatalarını kendi oncall
> agent'ı izleyen kişisel asistan.

---

## 1. Amaç ve kapsam

**Çözdüğü sorun:** Alışveriş listesi, günlük yapılacaklar ve alışkanlık takibi farklı
uygulamalara dağılıyor; not almak sürtünme yaratıyor. Tek bir sohbete doğal dille yazmak
yeterli olmalı.

**v1 kapsamında:**
- Telegram botu (tek kullanıcı: ben)
- Router + 2 agent: `shopping`, `tasks` (yapılacaklar + alışkanlıklar)
- Gece scheduler'ı (alışkanlıklardan günlük görev üretir) ve akşam özeti
- Her adımın trace'lenmesi
- Oncall agent (teknik + anlamsal hatalar)
- **Kolay kurulum:** repoyu klonlayan biri 2 komutla botu ayağa kaldırabilmeli (bkz. §12)
- **Çoklu LLM:** Anthropic, Gemini, DeepSeek, OpenAI; hangi anahtar varsa o kullanılır (bkz. §5)

**v1 kapsamı dışında (sonra):** WhatsApp, çok kullanıcı, sesli mesaj, belge/RAG agent'ı,
harcama agent'ı, web arayüzü.

---

## 2. Örnek konuşmalar (kabul kriterleri)

| Mesaj | Beklenen davranış |
|---|---|
| `zeytinyağı almam lazım` | shopping → "zeytinyağı" eklenir |
| `zeytinyağı ve 2 paket makarna` | shopping → 2 ürün, makarna adet=2 |
| `listede ne var?` | shopping → liste gösterilir |
| `makarnayı aldım` | shopping → makarna "alındı" |
| `bundan sonra günde 1 saat ingilizce çalışacağım` | tasks → alışkanlık: İngilizce, 60 dk/gün |
| `bugün 30 dk ingilizce çalıştım` | tasks → bugünkü İngilizce 30/60 |
| `yarın faturayı öde` | tasks → yarına tek seferlik görev |
| `bugün ne kaldı?` | tasks → kalan görevler |
| `zeytinyağı al, bir de bugün 20 dk ingilizce çalıştım` | router mesajı **ikiye böler** → shopping + tasks |
| `hmm` / anlamsız | "Ne yapmamı istediğini anlayamadım" + örnekler |
| `ingilizce'yi bırakıyorum` | tasks → **onay sorar**, sonra alışkanlığı pasif yapar |

Bu tablo aynı zamanda ilk **eval setinin** çekirdeği (bkz. §9).

---

## 3. Mimari

```
Telegram ──▶ Gateway ──▶ Router ──▶ [Agent 1..N] ──▶ Tool fonksiyonları ──▶ SQLite
               │           │             │
               │           └──── LLM katmanı (sağlayıcıdan bağımsız) ────┘
               │
               └── her mesaja trace_id ── tüm adımlar ──▶ Trace store ──▶ Oncall agent ──▶ Telegram (ayrı sohbet)

Scheduler (APScheduler): 00:00 günlük görev üretimi · 21:00 akşam özeti · 08:00 oncall günlük rapor
```

**Temel ilke:** LLM *karar verir* (yapılandırılmış JSON / tool çağrısı), *iş kodla yapılır*.
Veritabanına yazmak, tarih hesaplamak, liste biçimlendirmek deterministik Python kodudur.

### Bileşenler

| Bileşen | Görev |
|---|---|
| `gateway/telegram.py` | Mesajı alır, izinli kullanıcı mı kontrol eder, `trace_id` üretir, cevabı gönderir |
| `core/router.py` | Kayıtlı agent'ların `description` + örneklerinden prompt kurar; çıktı: `[{agent, text}]` listesi (birleşik mesajları böler) |
| `core/runtime.py` | Seçilen agent için tool-calling döngüsünü çalıştırır (maks. 5 adım) |
| `core/registry.py` | `agents/` klasörünü tarar, her agent'ı otomatik yükler |
| `core/llm/` | Tek arayüz: `complete(system, messages, tools)`. Arkada model değiştirilebilir (bkz. §5) |
| `core/memory.py` | Son 10 mesaj (kısa süreli) + `facts` tablosu (uzun süreli) |
| `core/tracing.py` | Her adımı `traces` tablosuna yazar (bkz. §8) |
| `scheduler.py` | Zamanlanmış işler |
| `oncall/` | Oncall agent'ı (kendisi de bir agent, ama ayrı kanaldan konuşur) |

---

## 4. Agent eklentisi sistemi

**Hedef:** yeni agent = yeni klasör. Çekirdek koda dokunulmaz.

```
agents/
  shopping/
    agent.yaml      # kimlik, açıklama, örnekler, prompt
    tools.py        # @tool ile işaretli Python fonksiyonları
    models.py       # (opsiyonel) bu agent'ın tabloları
    evals.yaml      # bu agent'ın test mesajları ve beklenen sonuçlar
  tasks/
    ...
```

### `agent.yaml`

```yaml
name: shopping
description: "Alışveriş listesini yönetir: ürün ekler, çıkarır, alındı işaretler, listeyi gösterir."
examples:              # router'a few-shot örnek olarak gider
  - "süt almam lazım"
  - "listede ne var"
  - "ekmeği aldım"
model: default         # veya belirli bir model (bkz. §5)
confirm_tools: [clear_list]   # bu tool'lar çalışmadan önce kullanıcıdan onay alınır
system_prompt: |
  Sen kullanıcının alışveriş listesini yöneten asistansın. Ürün adlarını
  yalın halde kaydet ("zeytinyağını" → "zeytinyağı"). Kısa ve samimi cevap ver.
```

### `tools.py`

```python
from core.tools import tool, ToolContext

@tool
def add_items(ctx: ToolContext, items: list[Item]) -> str:
    """Listeye bir veya daha fazla ürün ekler."""
    ...

@tool
def list_items(ctx: ToolContext, only_pending: bool = True) -> str:
    """Listedeki ürünleri döndürür."""
    ...
```

- `@tool` dekoratörü fonksiyonun tip ipuçlarından ve docstring'inden **JSON schema'yı otomatik üretir** (Pydantic).
- `ToolContext` her tool'a `user_id`, `trace_id`, `db`, `now` (saat dilimli) verir. Böylece tool'lar test edilebilir kalır.
- Agent'lar birbirinin tablosuna **doğrudan** yazmaz. Gerekirse başka agent'ın tool'unu `ctx.call("tasks.add_task", ...)` ile çağırır (v2).

### Yeni agent ekleme adımları

1. `agents/<isim>/` klasörünü kopyala (`agents/_template`)
2. `agent.yaml` doldur, `tools.py` yaz
3. `evals.yaml`'a 5–10 örnek mesaj ekle
4. `make eval AGENT=<isim>` → router doğru yönlendiriyor mu, tool'lar doğru çağrılıyor mu
5. Botu yeniden başlat; registry otomatik yükler

---

## 5. Model (LLM) seçimi

### Sağlayıcı seçimi: anahtar neredeyse o

`.env.example` içinde dört anahtar da **boş** gelir. Kullanıcı hangisini doldurursa o kullanılır.
Birden fazlası doluysa öncelik sırası:

```
1. ANTHROPIC_API_KEY  → claude-haiku-5-5
2. GEMINI_API_KEY     → gemini-3.1-flash-lite
3. DEEPSEEK_API_KEY   → deepseek-flash
4. OPENAI_API_KEY     → gpt-6-luna
```

- `LLM_PROVIDER=deepseek` → sıralamayı ezer, zorla o sağlayıcı (anahtarı yoksa açık hata)
- `LLM_MODEL=...` → sağlayıcının varsayılan modelini ezer
- Hiç anahtar yoksa uygulama **başlamaz** ve ne yapılacağını söyler:
  `LLM anahtarı bulunamadı. .env dosyasında ANTHROPIC_API_KEY, GEMINI_API_KEY, DEEPSEEK_API_KEY veya OPENAI_API_KEY değerlerinden birini doldurun.`
- Açılışta tek satır log: `LLM: deepseek / deepseek-flash (DEEPSEEK_API_KEY bulundu)`
- Telegram'da `/status` → aktif sağlayıcı, model, bugünkü token ve maliyet
- Varsayılan model adları `core/llm/defaults.py` içinde tek yerde durur; yeni model çıkınca tek satır değişir

### Mimari: iki adaptör, dört sağlayıcı

```
erranderpy/core/llm/
  base.py              # ortak tipler + LLMProvider arayüzü: complete(system, messages, tools) -> LLMResult
  defaults.py          # sağlayıcı listesi, öncelik, varsayılan modeller, sağlayıcı tuhaflıkları
  openai_compat.py     # OpenAI, DeepSeek ve Gemini (OpenAI uyumlu uç noktası)
  anthropic_native.py  # Anthropic native SDK (tool calling formatı farklı)
  errors.py            # SDK hataları → ortak hata türleri (auth, rate_limit, timeout, ...)
  select.py            # .env'e bakıp sağlayıcıyı seçer
  pricing.py           # model başına fiyat → trace'lerde maliyet hesabı
```

- `LLMResult` sağlayıcıdan bağımsız: `message` (metin + tool çağrıları), `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`
- Agent'lar ve router sağlayıcıyı **bilmez**; sadece `LLMProvider` ile konuşur
- `temperature` yok; karar çağrılarında her sağlayıcıda reasoning en düşük seviyede (ADR-0007)
- `tool_choice` sadece `auto` / `none`; tool çağrısı prompt'la istenir, sonuç doğrulanır (ADR-0006)
- Her adaptör aynı sözleşme testlerinden geçer (`tests/live/test_contract.py`, `make test-live`): düz cevap, tool çağrısı, tool sonucu döngüsü
- **Kademeli model (v2):** `agent.yaml → model` ile belirli bir agent'a daha güçlü model verilebilir

### Model karşılaştırması (Aşama 0)

`make compare` → `.env`'de anahtarı bulunan **tüm** sağlayıcılarda Türkçe eval setini
(`evals/router_tr.yaml` 30 örnek, `evals/tools_tr.yaml` 16 örnek) koşar; doğruluk, hata oranı,
p50/p95 gecikme ve çağrı başı maliyeti tablo halinde basar (`reports/eval-<tarih>.md`).
Sonuç README'de yayınlanır.

---

## 6. Veri modeli (SQLite)

```
shopping_items  id, name, qty, unit, status(pending|bought|removed), created_at, bought_at
habits          id, name, daily_target_min, active, created_at
tasks           id, date, title, habit_id NULL, target_min NULL, done_min, status(open|done|skipped)
facts           id, key, value, source_trace_id, updated_at      -- uzun süreli hafıza
messages        id, chat_id, role, text, trace_id, created_at     -- kısa süreli hafıza
traces          id, trace_id, step, agent, input, output, tool, latency_ms, tokens_in, tokens_out, cost_usd, error, created_at
feedback        id, trace_id, kind(correction|thumbs_down), text, created_at
```

- Saat dilimi: `Europe/Istanbul`; tüm "bugün/yarın" hesapları kodda yapılır, LLM'e bırakılmaz.
- Gece 00:00: aktif her alışkanlık için o güne `tasks` kaydı oluşturulur (idempotent).

---

## 7. Hata durumları ve güvenlik

| Durum | Davranış |
|---|---|
| Router emin değil | Kullanıcıya seçenekli soru sorar (Telegram butonları) |
| LLM geçersiz JSON / yanlış tool argümanı | Hata mesajıyla 1 kez yeniden dene; yine olmazsa özür + trace'e `error` |
| LLM API ulaşılamıyor / timeout | "Şu an cevap veremiyorum, mesajını kaydettim" → mesaj kuyruğa, sonra tekrar işlenir |
| Silme / temizleme | `confirm_tools` → onay butonu |
| Yabancı kullanıcı yazarsa | Yok sayılır. Sahip, kurulumda **eşleştirme koduyla** belirlenir (bkz. §12) |
| Kullanıcı düzeltirse ("hayır öyle demedim", "geri al") | Son işlem geri alınır + `feedback` kaydı (oncall için sinyal) |

API anahtarları `.env`'de; repo'ya girmez.

---

## 8. Gözlemlenebilirlik

Her mesaj için tek `trace_id`; şu adımlar `traces` tablosuna yazılır:
`gateway.receive → router.decide → agent.llm_call → agent.tool_call → gateway.reply`

Her adımda: girdi/çıktı (kısaltılmış), süre, token, maliyet, hata.
v1'de kendi SQLite tablomuz yeterli; v2'de OpenTelemetry → Langfuse veya New Relic'e aktarılır.

---

## 9. Eval

- `evals/*.yaml` + her agent'ın `evals.yaml`'ı: `{mesaj, beklenen_agent, beklenen_tool, beklenen_argümanlar}`
- `make eval` → tüm seti koşar, doğruluk / hata oranı / ortalama gecikme / maliyet raporu basar
- `make eval MODEL=...` → model karşılaştırması
- Gerçek kullanımdaki her `feedback` kaydı yeni bir eval örneğine dönüştürülür

---

## 10. Oncall agent

**Ne izler:**
- Teknik: exception'lar, LLM API hata/timeout oranı, p95 gecikme, scheduler çalıştı mı, günlük maliyet
- Anlamsal: `feedback` kayıtları, router'ın "bilinmeyen" dediği mesajlar, aynı mesajın kısa sürede tekrar yazılması

**Tetikleyiciler:**
- Anlık: kritik hata (ör. art arda 3 LLM hatası, scheduler kaçtı) → hemen araştır
- Günlük 08:00: son 24 saatin raporu + gece eval sonucu

**Tool'ları (salt okunur):**
`query_traces(filter)`, `get_trace(trace_id)`, `get_errors(since)`, `get_feedback(since)`,
`get_agent_config(name)`, `run_eval(agent)`, `get_recent_commits()`, `llm_health()`

**Yazma yetkisi yok.** Çözüm önerir (ör. prompt'a eklenecek örnek); uygulamak benim onayımla.

**Çıktı örneği:**
> ⚠️ Son 24 saatte 3 mesaj yanlış agent'a gitti; hepsi "…çalıştım" kalıbında, router
> yeni görev sandı. Öneri: `tasks/agent.yaml` örneklerine ilerleme kaydı cümleleri eklemek.
> Eval'de bu kalıp 2/5 başarılı.

**Test:** hata enjeksiyonu (`make chaos`): LLM anahtarını boz, scheduler'ı durdur, prompt'u
bozan bir commit at → oncall doğru teşhisi koyuyor mu?

---

## 11. Teknoloji

Python 3.12 · `python-telegram-bot` (polling) · SQLite (+ SQLModel) · APScheduler · Pydantic ·
`openai` + `anthropic` SDK'ları · pytest · `uv` (Python ve paket yönetimi) · Docker Compose (VPS için)

Dış servis yok: veritabanı tek bir dosya (`data/assistant.db`), ayrı bir sunucu kurulmaz.

---

## 12. Kurulum ve çalıştırma

**Hedef:** repoyu ilk kez gören biri, sadece bir Telegram bot token'ı ve bir LLM anahtarıyla
5 dakikada botla konuşuyor olmalı. Python bilmesi gerekmemeli.

### Yerel (Mac / Linux): 2 komut

```bash
git clone https://github.com/<kullanici>/<repo>.git && cd <repo>
./start.sh
```

`start.sh` sırasıyla şunları yapar:
1. `uv` yoksa kurar (uv, doğru Python sürümünü de kendisi indirir; sistemde Python gerekmez)
2. Bağımlılıkları kurar (`uv sync`)
3. `.env` yoksa **kurulum sihirbazını** açar:
   - Telegram bot token'ı ister (BotFather'dan nasıl alınacağını 3 satırla anlatır)
   - LLM anahtarını ister: "Hangisi var? 1) Anthropic 2) Gemini 3) DeepSeek 4) OpenAI"
   - Anahtarı küçük bir test çağrısıyla **doğrular**, hatalıysa tekrar sorar
   - `.env` dosyasını yazar
4. Veritabanını oluşturur / migrate eder
5. **Eşleştirme:** terminale 6 haneli kod basar → "Botuna Telegram'dan bu kodu gönder".
   Kodu gönderen hesap sahip olarak kaydedilir; başka kimse botu kullanamaz.
6. Botu başlatır, terminale `✅ Hazır. Telegram'dan yazabilirsin.` basar

Sonraki çalıştırmalarda `./start.sh` sihirbazı atlar, doğrudan başlatır.

### Ek komutlar (`Makefile`)

| Komut | Ne yapar |
|---|---|
| `make doctor` | `.env`, LLM anahtarı, Telegram token'ı, veritabanı, saat dilimi kontrolü; sorunları açık Türkçe/İngilizce yazar |
| `make eval` / `make compare` | Eval seti / model karşılaştırması |
| `make new-agent NAME=x` | `agents/_template`'ten yeni agent klasörü |
| `make chaos` | Oncall testi için hata enjeksiyonu |
| `make service` | Arka plan servisi olarak kur: Mac'te launchd, Linux'ta systemd. Makine açılınca / bot çökünce otomatik başlar |

### VPS: iki yol, Docker zorunlu değil

**Yol A — Docker'sız (varsayılan, öneri):** Mac'teki ile aynı adımlar.

```bash
git clone ... && cd <repo>
./start.sh          # sihirbaz + eşleştirme; çalıştığını gör, Ctrl+C
make service        # systemd servisi kur → arka planda, reboot sonrası da çalışır
```

- Gereken tek şey `git` ve `curl` (neredeyse her Linux VPS'te var); Python'u uv indirir
- Loglar: `journalctl -u <servis-adi> -f`

**Yol B — Docker Compose (Docker zaten kuruluysa):**

```bash
git clone ... && cd <repo>
cp .env.example .env   # anahtarları doldur
docker compose up -d
```

- `data/` klasörü volume olarak bağlanır; konteyner silinse de veriler kalır
- Eşleştirme kodu `docker compose logs` içinde görünür

**Her iki yolda da:**
- Telegram polling kullandığı için bot **hiçbir port açmaz**; alan adı, HTTPS, nginx ayarı gerekmez
- VPS'te çalışan nginx veya başka servislerle çakışmaz
- Bellek kullanımı küçüktür (tahmini 100–200 MB)
- `make doctor` VPS'te de çalışır

**İleride nginx gerekirse:** Telegram'ı webhook moduna almak veya bir web paneli eklemek
istenirse, bot `127.0.0.1:<port>`'ta dinler ve mevcut nginx'e bir `location` bloğu eklenir
(örnek config `deploy/nginx.conf.example` olarak repoda durur). v1'de buna gerek yok.

### "Kolay kurulum" testi

Her sürümden önce: temiz bir klasöre (veya temiz bir Docker konteynerine) klonla, sadece
README'deki adımları izle. 5 dakikayı geçiyorsa veya README dışı bir şey gerekiyorsa hata sayılır.
CI'da da otomatik koşar (GitHub Actions: klonla → sahte anahtarlarla `make doctor` → testler).

### Mac'te dikkat

Mac uykuya geçince bot da durur. `make service` launchd ile kurar; Mac uyandığında bot kendiliğinden
devam eder (polling kaçan mesajları da alır). Kesintisiz çalışma isteyen kullanıcı VPS'e geçer.

---

## 13. Aşamalar

| Aşama | Çıktı | "Bitti" tanımı |
|---|---|---|
| **0. LLM katmanı + model seçimi** | 2 adaptör, sağlayıcı seçimi, 30 mesajlık Türkçe eval seti, `make compare` | 4 sağlayıcı sözleşme testlerini geçiyor, karşılaştırma raporu hazır |
| **1. Çekirdek + shopping + kurulum** | `start.sh`, sihirbaz, eşleştirme, Telegram, registry, router, runtime, tracing, shopping agent | Temiz klondan 2 komutla bot çalışıyor; §2'deki alışveriş satırları geçiyor |
| **2. tasks + scheduler** | tasks agent, alışkanlıklar, gece işi, akşam özeti | §2'nin tamamı geçiyor, eval ≥ %90 |
| **3. Oncall** | oncall agent, günlük rapor, chaos testleri | 5 enjekte hatanın ≥ 4'ünü doğru teşhis |
| **4. Platformlaşma + yayın** | `make new-agent`, 3. agent'ı 30 dakikada ekleme, Docker, README, demo videosu | Yeni agent çekirdeğe dokunmadan eklendi; repo herkese açık |

---

## 14. Kararlar

- [x] Çalışma yeri: önce kendi Mac'im, sonra aynı repodan VPS (Docker'sız `start.sh` + systemd; Docker opsiyonel)
- [x] Telegram, polling modu
- [x] Çoklu LLM: Anthropic → Gemini → DeepSeek → OpenAI önceliği, `LLM_PROVIDER` ile ezilebilir
- [x] Kurulum: `./start.sh` + sihirbaz + eşleştirme kodu
- [ ] Varsayılan model (Aşama 0 sonucuna göre; sıralama değişebilir)
- [x] Dil: kod, README ve commit'ler İngilizce; SPEC Türkçe; bot kullanıcının dilinde cevap verir
- [x] Proje adı `erranderpy`, lisans MIT, repo herkese açık: https://github.com/firatkill/erranderpy
