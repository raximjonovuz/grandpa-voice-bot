/**
 * Cloudflare Worker - Telegram Webhook Gateway, Proxy & Uptime Pinger
 * 1. Har 5 daqiqada Render backendini ping qilib uxlashdan saqlaydi (Cron Trigger)
 * 2. Telegram so'rovlarini 100% ishonchli qabul qiladi va Render'ga uzatadi
 */

async function sha256Hex(text) {
  const data = new TextEncoder().encode(text);
  const hash = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(hash)].map(b => b.toString(16).padStart(2, "0")).join("");
}

export default {
  // Cron trigger (Har 5 daqiqada Render backend'ini uyg'oq ushlab turish)
  async scheduled(event, env, ctx) {
    try {
      const pingUrl = `${env.BACKEND_URL}/health`;
      const res = await fetch(pingUrl, { method: "GET" });
      console.log(`[Uptime Ping] Status: ${res.status}`);
    } catch (e) {
      console.error(`[Uptime Ping Error]: ${e.message}`);
    }
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Health check endpoint
    if (url.pathname === "/" || url.pathname === "/health") {
      // Backend holatini ham tekshirib qaytarish
      let backendStatus = "unknown";
      try {
        const ping = await fetch(`${env.BACKEND_URL}/health`, { method: "GET" });
        backendStatus = ping.status === 200 ? "alive" : `http_${ping.status}`;
      } catch (e) {
        backendStatus = "unreachable";
      }

      return new Response(
        JSON.stringify({
          status: "ok",
          platform: "Cloudflare Workers",
          cron_uptime: "active_every_5_min",
          backend: backendStatus,
          time: new Date().toISOString()
        }),
        {
          headers: { "Content-Type": "application/json" }
        }
      );
    }

    // Telegram webhook endpoint: /webhook yoki /webhook/:token
    if (request.method === "POST" && url.pathname.startsWith("/webhook")) {
      const backendUrl = `${env.BACKEND_URL}/webhook/${env.BOT_TOKEN}`;

      // Backend bilan bir xil maxfiy kalit: WEBHOOK_SECRET yoki sha256(BOT_TOKEN)
      const secret = env.WEBHOOK_SECRET || await sha256Hex(env.BOT_TOKEN);

      // Faqat Telegram (maxfiy kalitni biladigan) so'rovlarini qabul qilamiz
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== secret) {
        return new Response("Forbidden", { status: 403 });
      }

      try {
        const body = await request.clone().arrayBuffer();

        // Render backend'iga yuborish
        const response = await fetch(backendUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": secret
          },
          body: body
        });

        return new Response(response.body, {
          status: response.status,
          headers: response.headers
        });
      } catch (err) {
        // Backend vaqtincha uxlab yotgan bo'lsa ham Telegramga 200 OK qaytaramiz
        return new Response(JSON.stringify({ ok: true, forwarded: false, error: err.message }), {
          status: 200,
          headers: { "Content-Type": "application/json" }
        });
      }
    }

    return new Response("Not Found", { status: 404 });
  }
};
