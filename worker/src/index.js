/**
 * Cloudflare Worker - Telegram Webhook Gateway & Proxy
 * Barcha Telegram so'rovlarini 100% ishonchli qabul qiladi,
 * uxlab qolmaydi va Render backend'iga yo'naltiradi.
 */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Health check endpoint
    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response(
        JSON.stringify({
          status: "ok",
          platform: "Cloudflare Workers",
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
      
      try {
        const body = await request.clone().arrayBuffer();

        // Render backend'iga yuborish
        const response = await fetch(backendUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: body
        });

        return new Response(response.body, {
          status: response.status,
          headers: response.headers
        });
      } catch (err) {
        // Backend vaqtincha uxlab yotgan bo'lsa ham Telegramga 200 OK qaytaramiz
        // (Telegram so'rovni qayta-qayta yuborib ban qilib qo'ymasligi uchun)
        return new Response(JSON.stringify({ ok: true, forwarded: false, error: err.message }), {
          status: 200,
          headers: { "Content-Type": "application/json" }
        });
      }
    }

    return new Response("Not Found", { status: 404 });
  }
};
