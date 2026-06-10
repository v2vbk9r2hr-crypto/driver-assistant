require("dotenv").config();

const express = require("express");
const cors = require("cors");
const { createClient } = require("@supabase/supabase-js");

const app = express();

app.use(cors());
app.use(express.json());
app.use(express.static("public"));

const PORT = process.env.PORT || 3000;

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;

if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
  console.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY");
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

app.get("/", (req, res) => {
  res.redirect("/driver.html");
});

app.get("/api/driver-assistant/orders", async (req, res) => {
  try {
    const { data, error } = await supabase
      .from("driver_assistant_orders")
      .select("*")
      .eq("status", "open")
      .order("created_at", { ascending: false })
      .limit(50);

    if (error) throw error;

    res.json({
      ok: true,
      orders: data || []
    });
  } catch (err) {
    console.error("get driver assistant orders error:", err);

    res.status(500).json({
      ok: false,
      error: err.message
    });
  }
});

app.get("/health", (req, res) => {
  res.json({
    ok: true,
    service: "driver-assistant",
    time: new Date().toISOString()
  });
});

app.post("/api/driver/location", async (req, res) => {
  try {
    const { driver_key, plate, lat, lng, accuracy } = req.body;

    if (!driver_key || !plate || lat == null || lng == null) {
      return res.status(400).json({
        ok: false,
        error: "missing fields"
      });
    }

    const { error } = await supabase
      .from("driver_locations")
      .upsert(
        {
          driver_key,
          plate,
          lat,
          lng,
          accuracy,
          online: true,
          updated_at: new Date().toISOString()
        },
        {
          onConflict: "driver_key"
        }
      );

    if (error) throw error;

    res.json({ ok: true });
  } catch (err) {
    console.error("driver location error:", err);
    res.status(500).json({
      ok: false,
      error: err.message
    });
  }
});

app.post("/api/driver/offline", async (req, res) => {
  try {
    const { driver_key } = req.body;

    if (!driver_key) {
      return res.status(400).json({
        ok: false,
        error: "missing driver_key"
      });
    }

    const { error } = await supabase
      .from("driver_locations")
      .update({
        online: false,
        updated_at: new Date().toISOString()
      })
      .eq("driver_key", driver_key);

    if (error) throw error;

    res.json({ ok: true });
  } catch (err) {
    console.error("driver offline error:", err);
    res.status(500).json({
      ok: false,
      error: err.message
    });
  }
});

app.get("/api/drivers/online", async (req, res) => {
  try {
    const fiveMinutesAgo = new Date(Date.now() - 5 * 60 * 1000).toISOString();

    const { data, error } = await supabase
      .from("driver_locations")
      .select("plate, lat, lng, accuracy, online, updated_at")
      .eq("online", true)
      .gte("updated_at", fiveMinutesAgo)
      .order("updated_at", { ascending: false });

    if (error) throw error;

    res.json({
      ok: true,
      count: data.length,
      drivers: data
    });
  } catch (err) {
    console.error("online drivers error:", err);
    res.status(500).json({
      ok: false,
      error: err.message
    });
  }
});

app.listen(PORT, () => {
  console.log(`Driver Assistant running on port ${PORT}`);
});