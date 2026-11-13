#pragma once

namespace argus::backend::television {

// Registers GET /api/tv/channels and GET /api/tv/videos on the global
// drogon::app(). Call once, before app().run().
void registerRoutes();

}  // namespace argus::backend::television
