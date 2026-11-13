#pragma once

namespace argus::backend::database {

// Reads and validates database configuration, creates a non-blocking Drogon
// PostgreSQL pool, and registers /ready and /health/db.
void configure();

}  // namespace argus::backend::database
