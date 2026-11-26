CREATE TABLE IF NOT EXISTS leads (
 id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
 name TEXT NOT NULL, company TEXT NOT NULL, email TEXT NOT NULL, message TEXT NOT NULL,
 created INTEGER NOT NULL, followup TEXT NOT NULL DEFAULT 'new' CHECK(followup IN ('new','progress','closed'))
);
CREATE INDEX IF NOT EXISTS leads_recent ON leads(created DESC,id);
CREATE INDEX IF NOT EXISTS leads_email ON leads(email COLLATE NOCASE,created);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY REFERENCES leads(id) ON DELETE CASCADE,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','retry','processing','accepted','delivered','delayed','failed','bounced','uncertain')),
 attempts INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 1, due INTEGER NOT NULL,
 lease INTEGER, token TEXT, first_attempt INTEGER, provider_id TEXT UNIQUE, error TEXT, payload TEXT, event_stamp INTEGER
);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(state,due,lease);
CREATE TABLE IF NOT EXISTS history (
 n INTEGER PRIMARY KEY, id TEXT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
 ts INTEGER NOT NULL, label TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS history_id ON history(id,n);
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, stamp INTEGER NOT NULL, state TEXT NOT NULL, received INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS events_provider ON events(provider_id,stamp);
CREATE TABLE IF NOT EXISTS login_limit (slot INTEGER PRIMARY KEY, attempts INTEGER NOT NULL);
