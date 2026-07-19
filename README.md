# **astronomy-multi-catalog-cross-matcher**

## **Table of Contents**

1. [App Description](#i-app-description)
2. [App & User Workflow](#ii-app--user-workflow)
3. [Accounts, Favorites & Personal AI Summaries](#iii-accounts-favorites--personal-ai-summaries)
4. [Real World Examples](#iv-real-world-examples)
5. [Database Schema](#v-database-schema)
6. [FAQ](#vi-faq)
7. [Third-Party Data & Services](#vii-third-party-data--services)

## **I. App Description**

An FastAPI WebApp that takes an unstructured, informally-typed star name, resolves it deterministically across independent astronomical catalogs via a cross-identification pipeline, and then, as a distinct, separate step, translates the resulting structured data into a plain-language explanation a non-specialist can actually read.

**Architectural principle:** the resolution engine (deterministic, SQL/ADQL-driven) and the narrative layer (AI-generated) are kept strictly separate: the AI layer can never affect matching logic or introduce a factual error the SQL layer didn't already contain.

The resolver, cross-matcher, and AI narrative are fully usable anonymously. An optional account layer sits on top for anyone who wants to save objects and keep a personal copy of the AI narratives they generate. See [Section III](#iii-accounts-favorites--personal-ai-summaries).

## **II. App & User Workflow**

User types in an informal star name ("51 Peg", "HD 217014", even slightly messy input), and it:

1. **Normalises** the query (fixes whitespace/casing, canonicalizes catalog prefixes like HD/HIP/GJ/TYC without stripping them)
2. **[Resolves](#a-resolve-explanation)** identity against SIMBAD (the standard astronomical object database) via a TAP/ADQL query, pulling the canonical name, coordinates, spectral type, and every known alias in one round trip
3. **Cross-matches** those aliases against the NASA Exoplanet Archive in a single batched query (not one HTTP request per alias) to find any known planets orbiting that star
4. **Classifies** the result into one of [five explicit states](#b-five-states-explanation) — `RESOLVED`, `PARTIAL`, `AMBIGUOUS`, `UNRESOLVED`, `LOOKUP_FAILED` — rather than quietly picking one answer or silently failing.
5. **Caches** the result with a 14-day TTL (1 hour for failed/ambiguous lookups, so bugs self-heal quickly)
6. **Renders** the result page immediately with the scientific data. The plain-English AI narrative (via the Gemini API) is generated separately on request, via a "Generate AI summary" button. Therefore, a slow Gemini call never blocks the page it's summarizing. Once generated, the narrative is cached the same way the scientific data is, and stays kept strictly separate from it: the AI layer can never corrupt or override what the SQL layer already established.

Programmatic access is also available via `GET /api/resolve?q=...`, which returns the same resolution data as JSON instead of HTML.

## **III. Accounts, Favorites & Personal AI Summaries**

Accounts are entirely optional; every step above works the same for an anonymous visitor. Logging in (session-cookie based, via email + password) adds three things on top:

- **Saving objects.** A logged-in user can favorite/unfavorite any resolved or partial object from its result page. Their saved list lives at `/account/saved`.
- **Login redirects for save actions.** If an anonymous visitor tries to favorite or unfavorite an object, the app sends them to `/login` first and returns them to the object page afterward.
- **A personal copy of "your" AI summary.** Logged-in users get a saved snapshot of the shared summary. See FAQ C for details.
- **Fair-use protection on AI generation**, enforced in two independent layers:
  1. **Per-object cooldown (5 minutes).** Stops rapid double-clicking Regenerate on the *same* object. Applies regardless of login state.
  2. **Per-client rate limit (20 requests/hour).** Stops one client — a logged-in user, or an anonymous browser identified by its session cookie — from spending Gemini quota by clicking Generate across *many different* objects in a short window, which the per-object cooldown alone doesn't prevent.

See the FAQ for why `/history` stays global, why anonymous save actions redirect to login, and why CSRF protection is still a known limitation.

## **IV. Real-world examples**

| Search | Expected state | Why |
| --- | --- | --- |
| `"51 Peg"` | RESOLVED | Famous exoplanet host, well-catalogued |
| `"51 Pegasi"` | RESOLVED | Same star, different name — should resolve to same planet |
| `"HD 217014"` | RESOLVED | Catalog ID for the same star — should match via alias lookup |
| `"Betelgeuse"` | PARTIAL | Real, famous star, but no catalogued planets |
| `"Proxima Centauri"` | RESOLVED | Closest star to the Sun, has confirmed exoplanet(s) |
| `"The Sun"` or `"Sol"` | UNRESOLVED | (Probably — SIMBAD might not index "Sol" as an alternate name) |
| `"Beta Cen"` | AMBIGUOUS | (Possibly — if SIMBAD lists both the primary and companion) |
| `"asdfjkl"` | UNRESOLVED | Gibberish |
| `"HD 217014"` (SIMBAD unreachable — network timeout, firewalled host, etc.) | LOOKUP_FAILED | SIMBAD was never actually reached, so this is not a real "no match" |

## **V. Database Schema**

The application utilizes a relational structure (SQLAlchemy) to cache SIMBAD resolutions, cross-matched planet data, and AI-generated narratives, plus (optionally) accounts, favorites, personal summary snapshots, and rate-limit bookkeeping.

<details>
<summary><b>View Database Table Definitions (Click to expand)</b></summary>

### 1. The `objects` Table

This is the core table of the application. It caches the primary astronomical data fetched from SIMBAD and tracks the lifecycle of the search query.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **simbad_main_id** | VARCHAR | Yes | UNIQUE |
| **query_text** | VARCHAR | No | |
| **ra_deg** | FLOAT | Yes | Right Ascension (degrees) |
| **dec_deg** | FLOAT | Yes | Declination (degrees) |
| **otype** | VARCHAR | Yes | Object Type |
| **spectral_type** | VARCHAR | Yes | |
| **resolution_state** | VARCHAR | No | CHECK (`RESOLVED`, `AMBIGUOUS`, `PARTIAL`, `UNRESOLVED`, `LOOKUP_FAILED`) |
| **ai_summary** | TEXT | Yes | |
| **ai_summary_generated_at** | DATETIME | Yes | Set on real (re)generation only, never on a cache hit |
| **candidates_json** | TEXT | Yes | |
| **resolved_via_json** | TEXT | Yes | |
| **resolved_at** | DATETIME | No | |
| **expires_at** | DATETIME | No | |

#### Detailed Column Explanations for `objects`

- **`id`**: Unique internal auto-incrementing identifier for each master object record.
- **`simbad_main_id`**: The canonical, standard primary identifier returned by the SIMBAD database. Enforces a `UNIQUE` constraint so we never duplicate the same real-world celestial object in our cache.
- **`query_text`**: The exact, raw string typed by the user (e.g., `"51 peg"`). Crucial for checking if a casual user search hits an already cached query.
- **`ra_deg`**: Right Ascension converted to decimal degrees. Represents the celestial equivalent of longitude. Nullable if the object cannot be resolved or lacks spatial coordinates.
- **`dec_deg`**: Declination converted to decimal degrees. Represents the celestial equivalent of latitude.
- **`otype`**: The astronomical object classification returned by SIMBAD (e.g., Star, High proper-motion Star, White Dwarf).
- **`spectral_type`**: The spectral classification of the star (e.g., `G5V`), indicating its temperature, luminosity, and evolutionary stage.
- **`resolution_state`**: The core state engine value of the application. Restricted by a `CHECK` constraint to exactly five mutually exclusive states: `RESOLVED`, `AMBIGUOUS`, `PARTIAL`, `UNRESOLVED`, or `LOOKUP_FAILED`.
- **`ai_summary`**: The plain-language narrative generated by the Gemini API. Stored here safely as a string text layer so it cannot touch or corrupt the scientific coordinate float data.
- **`ai_summary_generated_at`**: Timestamp of the last real generation (initial Generate or a Regenerate) — never updated on a cache hit. Drives the 5-minute per-object cooldown on regeneration; null until a summary has been generated at least once.
- **`candidates_json`**: A stringified JSON array utilized when a state is `AMBIGUOUS`. It stores the basic data of multiple potential stellar matches so the UI can render a disambiguation selection list.
- **`resolved_via_json`**: A stringified JSON audit trail recording exactly how the app navigated from the raw query to the final match (e.g., `User Input -> SIMBAD Alias -> NASA Exoplanet ID`).
- **`resolved_at`**: The precise timestamp of when the external API lookup was performed and recorded.
- **`expires_at`**: The calculated cache expiration timestamp. Used by the background logic to enforce the 14-day TTL for successful resolutions and the 1-hour TTL for failed/ambiguous lookups.

---

### 2. The `identifiers` Table

Astronomical objects go by dozens of cross-catalog names. This table stores all known alternate aliases for a cached object, enabling secondary catalog cross-matching.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **object_id** | INTEGER | No | FOREIGN KEY (`objects.id`) ON DELETE CASCADE |
| **catalog** | VARCHAR | No | |
| **identifier** | VARCHAR | No | |
| **matched_exoplanet_archive** | BOOLEAN | No | |

#### Detailed Column Explanations for `identifiers`

- **`id`**: Internal unique identifier for the alias record.
- **`object_id`**: Foreign key linking the alias back to its parent record in the `objects` table. Includes `ON DELETE CASCADE` so if a cached object is cleared, its alias history is wiped automatically.
- **`catalog`**: The name of the specific star catalog recording this alias (e.g., `HD`, `HIP`, `TYC`).
- **`identifier`**: The specific designation/number assigned within that catalog (e.g., `217014`).
- **`matched_exoplanet_archive`**: A boolean flag indicating whether this specific alias successfully triggered a matching record in the NASA Exoplanet Archive database during the cross-identification process.

*Note: Enforces a composite `UNIQUE (object_id, catalog, identifier)` constraint to prevent duplicate alias mappings for the same object.*

---

### 3. The `planets` Table

Stores structural data for confirmed exoplanets tied to host stars, sourced from the NASA Exoplanet Archive.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **object_id** | INTEGER | No | FOREIGN KEY (`objects.id`) ON DELETE CASCADE |
| **pl_name** | VARCHAR | No | |
| **pl_letter** | VARCHAR | Yes | |
| **orbital_period_days** | FLOAT | Yes | |
| **planet_radius_earth** | FLOAT | Yes | |
| **discovery_year** | INTEGER | Yes | |
| **discovery_method** | VARCHAR | Yes | |

#### Detailed Column Explanations for `planets`

- **`id`**: Internal unique identifier for the exoplanet record.
- **`object_id`**: Foreign key linking the planet to its host star system in the `objects` table. Enforces `ON DELETE CASCADE`.
- **`pl_name`**: The official, complete canonical name of the exoplanet (e.g., `51 Peg b`).
- **`pl_letter`**: The lower-case letter designation assigned to the planet based on its order of discovery in the system (typically starting at `b`).
- **`orbital_period_days`**: The amount of time (measured in Earth days) the planet takes to complete one full revolution around its host star.
- **`planet_radius_earth`**: The physical size of the exoplanet expressed as a multiple of Earth's radius ($R_\oplus$).
- **`discovery_year`**: The calendar year the exoplanet's discovery was officially confirmed and published (e.g., `1995`).
- **`discovery_method`**: The scientific technique utilized by astronomers to detect the planet (e.g., `Radial Velocity`, `Transit`).

---

### 4. The `users` Table

Registered accounts. Session-cookie based auth (Starlette `SessionMiddleware`) — no separate session-token table.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **email** | VARCHAR | No | UNIQUE |
| **password_hash** | VARCHAR | No | bcrypt |
| **created_at** | DATETIME | No | |

#### Detailed Column Explanations for `users`

- **`id`**: Internal unique identifier for the account.
- **`email`**: Login identifier. `UNIQUE` constraint enforced at the database level — registration attempts an insert and catches the resulting integrity error rather than pre-checking existence with a separate query, avoiding a check-then-act race between two concurrent registrations for the same email.
- **`password_hash`**: Bcrypt hash of the password. The plaintext password is never stored or logged.
- **`created_at`**: Account creation timestamp.

---

### 5. The `saved_searches` Table

A logged-in user's favorited objects. Minimal MVP shape — just the link and a timestamp, no note/label field yet.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **user_id** | INTEGER | No | FOREIGN KEY (`users.id`) ON DELETE CASCADE |
| **object_id** | INTEGER | No | FOREIGN KEY (`objects.id`) ON DELETE CASCADE |
| **created_at** | DATETIME | No | |

#### Detailed Column Explanations for `saved_searches`

- **`id`**: Internal unique identifier for the favorite record.
- **`user_id`**: The user who favorited the object. `ON DELETE CASCADE` — deleting a user removes their favorites.
- **`object_id`**: The favorited object. `ON DELETE CASCADE` — deleting a cached object removes any favorites pointing at it.
- **`created_at`**: When the object was favorited; drives the ordering on `/account/saved` (most recent first).

*Note: Enforces a composite `UNIQUE (user_id, object_id)` constraint — a user can favorite a given object at most once. Re-favoriting an already-favorited object is treated as a no-op, not an error.*

---

### 6. The `user_summary_snapshots` Table

A logged-in user's personal copy of the AI summary they most recently generated/regenerated for a given object — the mechanism behind the ownership guarantee described in [Section III](#iii-accounts-favorites--personal-ai-summaries).

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **user_id** | INTEGER | No | FOREIGN KEY (`users.id`) ON DELETE CASCADE |
| **object_id** | INTEGER | No | FOREIGN KEY (`objects.id`) ON DELETE CASCADE |
| **summary_text** | TEXT | No | |
| **created_at** | DATETIME | No | |

#### Detailed Column Explanations for `user_summary_snapshots`

- **`id`**: Internal unique identifier for the snapshot record.
- **`user_id`** / **`object_id`**: Which user, and which object, this snapshot belongs to. Both `ON DELETE CASCADE`.
- **`summary_text`**: The AI narrative text as it existed at the moment this user generated/regenerated it. Entirely separate storage from `objects.ai_summary` — this table is never the source of the shared canonical summary, only a personal copy of it.
- **`created_at`**: When this user's snapshot was captured/last updated.

*Note: Enforces a composite `UNIQUE (user_id, object_id)` constraint — each user has at most one snapshot per object, their most recent own generation. A user regenerating overwrites their own snapshot; another user's regenerate never touches it.*

---

### 7. The `rate_limit_events` Table

An event log of Gemini-quota-spending actions (Generate/Regenerate clicks), used to enforce the per-client rate limit described in [Section III](#iii-accounts-favorites--personal-ai-summaries). Deliberately a log table rather than a counter column, so a sliding window can be computed by counting rows rather than resetting a counter on a timer.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **subject_type** | VARCHAR | No | CHECK (`user`, `session`) |
| **subject_id** | VARCHAR | No | |
| **created_at** | DATETIME | No | |

#### Detailed Column Explanations for `rate_limit_events`

- **`id`**: Internal unique identifier for the log entry.
- **`subject_type`**: `user` for a logged-in request (limited per `users.id`) or `session` for an anonymous request (limited per the Starlette session-cookie id, assigned to every visitor regardless of login state).
- **`subject_id`**: The `users.id` or session id this event counts against, as a string — not a foreign key to `users.id`, since one column needs to hold both kinds of identifier uniformly, and log rows should survive a user account being deleted rather than needing `ON DELETE` handling on what's really just an audit trail.
- **`created_at`**: When the request was made — the sliding window (20 requests/hour) is computed by counting rows newer than `now - 1 hour` for the same `(subject_type, subject_id)` pair.

</details>

## **VI. FAQ**

### **A. Resolve Explanation**

<details>
<summary><b>View Resolve Explanation (Click to expand)</b></summary>

**"Resolve"** = take an informal, ambiguous star name and turn it into a confirmed astronomical identity. The app is answering the question: "**Do we know what object the user is asking about, and can we find it across multiple independent catalogs?**"

Example: the user types `"51 peg"` (lowercase, casual spelling of a real star). The app resolves this by:

1. Normalising it to `"51 Peg"` (correct casing)
2. Asking SIMBAD: "Do you know an object called '51 Peg'?" → yes, it's `"51 Pegasi"` (canonical name)
3. Getting back aliases: `["HD 217014", "HIP 113357", "51 Peg", "51 Pegasi", ...]`
4. Asking NASA Exoplanet Archive: "Do any of these aliases host known planets?" → yes, `"HD 217014"` does
5. Getting back planet data: `51 Peg b` (an exoplanet, discovered 1995)

At each step, gains more certainty about what the user was asking for. "Resolve" is successfully completing that chain.

</details>

### **B. Five States Explanation**

<details>

<summary><b>View Five States Explanation (Click to expand)</b></summary>

- #### **RESOLVED** ✓✓

    **Meaning:** Identity confirmed *and* planets found.

    **What happened:** SIMBAD matched the query unambiguously (one object), and at least one of its aliases matched in the Exoplanet Archive.

    **What the user sees:** Full profile — spectral type, coordinates, orbital elements of its planets, the AI summary, a "resolved via: 51 Peg → 51 Pegasi → HD 217014 → [51 Peg b found]" trail showing the chain of lookups.

    **Example:** User searches `"51 Peg"` → RESOLVED (it's a real star with a real known planet).

- #### **PARTIAL** ✓✗

    **Meaning:** Identity confirmed, but no planets in the catalog.

    **What happened:** SIMBAD matched unambiguously (one object), but none of its aliases turned up anything in the Exoplanet Archive.

    **What the user sees:** Star's basic data (spectral type, coordinates, object type), but an explicit note: *"No planets were found in the Exoplanet Archive for this object."* (Important: this doesn't mean the star *has no* planets in reality — just that the NASA catalog doesn't list any, which is common for faint/distant/newly-discovered stars.)

    **Example:** User searches `"Barnard's Star"` (a real, nearby star) → PARTIAL (it's confirmed real, but the NASA archive hasn't catalogued any orbiting planets for it, though astronomers suspect there might be one).

- #### **AMBIGUOUS** ⚠️

    **Meaning:** The input matched multiple candidates — we can't safely guess which one you meant.

    **What happened:** SIMBAD returned more than one possible object for the query. (This happens surprisingly often with informal names.)

    **What the user sees:** A **disambiguation list**, showing each candidate with its canonical name, object type, spectral type, and coordinates — clickable links to re-search with the correct SIMBAD identifier, so the user can pick the right one and get a full profile.

    **Example:**

  - User searches `"51"` (way too vague) → AMBIGUOUS (there are hundreds of objects with "51" in their name)
  - User searches `"Beta Cen"` (could mean the primary star Beta Centauri or its close binary companion) → AMBIGUOUS (two distinct objects in SIMBAD)

- #### **UNRESOLVED** ✗

    **Meaning:** Query failed — SIMBAD returned nothing.

    **What happened:** Either the object genuinely doesn't exist in SIMBAD, or it was misspelled/malformed beyond recognition.

    **What the user sees:** A clear message: *"No SIMBAD match was found for this query."* Suggests checking spelling or trying a different identifier.

    **Example:**

  - User searches `"asdkfjhasdf"` (gibberish) → UNRESOLVED
  - User searches `"Foo's Nebula"` (made-up object) → UNRESOLVED
  - User searches `"Amphoreus"` (fiction) → UNRESOLVED

- #### **LOOKUP_FAILED** ⚠

    **Meaning:** The SIMBAD request itself could not be completed — this is not a verdict about the object at all.

    **What happened:** A transport-level failure (connection timeout, DNS failure, connection refused), a bad HTTP status from SIMBAD, or a response body that couldn't be parsed. Crucially, SIMBAD's TAP endpoint was never successfully queried, so nothing was actually checked.

    **Why this is a separate state from UNRESOLVED:** Early versions of this app treated every SIMBAD failure — timeouts included — the same as "no match found," which silently reported network problems as if the object didn't exist. A firewalled or unreachable network (e.g. some school/office networks block `simbad.cds.unistra.fr` outright) would then look identical to a genuinely nonexistent star.
  
    `LOOKUP_FAILED` keeps that distinction explicit.

    **What the user sees:** A message explaining that the *lookup* failed, not the object, along with a link to retry the same query.

    **Caching:** Same short, self-healing 1-hour TTL as `UNRESOLVED`/`AMBIGUOUS`, rather than the full 14-day TTL, so a transient network issue doesn't get "cached" as a wrong answer for two weeks. No AI summary is generated for this state, since there's no confirmed structured data to describe.

    **Example:**

  - User searches `"HD 217014"` from a network that can't route to SIMBAD at all → LOOKUP_FAILED
  - User searches `"51 Peg"` and SIMBAD's TAP service returns a 503 → LOOKUP_FAILED

#### **Decision Tree**

```text
Was SIMBAD actually reachable, and did it return a usable response?
├─ NO (timeout / transport error / bad status / unparseable response) → LOOKUP_FAILED (end)
└─ YES
   └─ Does SIMBAD know this object?
      ├─ NO  → UNRESOLVED (end)
      └─ YES, but is it ambiguous?
         ├─ YES (multiple candidates) → AMBIGUOUS (show list, end)
         └─ NO (one object)
            └─ Does Exoplanet Archive have planets for this object?
               ├─ YES → RESOLVED (show full profile)
               └─ NO  → PARTIAL (show star data, note no planets)
```

**Each state is mutually exclusive and exhaustive** — every possible search outcome falls into exactly one bucket.

</details>

### **C. "My saved summary looks different from what's shown to everyone else now — is that a bug?"**

<details>
<summary><b>View explanation (Click to expand)</b></summary>

No — this is expected, and it's the point of `user_summary_snapshots`.

There is exactly one canonical AI summary per object (`objects.ai_summary`), shown to every anonymous visitor and to any logged-in user who hasn't generated their own. If you're logged in and you personally clicked Generate or Regenerate, `/account/saved` shows *your* copy from that moment, even if someone else regenerates the shared version afterward.

This is an ownership/no-clobber guarantee, not the AI narrative varying its actual content by user. If you and another user both generate at the same point in time, from the same underlying data, you'll get the same text.

</details>

### **D. "Why two separate limits (a 5-minute cooldown AND a 20/hour rate limit) instead of just one?"**

<details>
<summary><b>View explanation (Click to expand)</b></summary>

They stop different failure modes:

- The **cooldown** is per-*object* — it stops rapid double-clicking Regenerate on the same star. It does nothing to stop someone clicking Generate on twenty different stars in a row.
- The **rate limit** is per-*client* (logged-in user, or anonymous session) — it stops exactly that: one visitor spending Gemini quota across many different objects in a short window, which the cooldown alone can't see, since it only ever looks at one object at a time.

Both checks run on every Generate/Regenerate request; either can reject it independently.

</details>

### **E. "Why is /history global instead of per-user?"**

<details>
<summary><b>View explanation (Click to expand)</b></summary>

`/history` is a site-wide feed of recently resolved objects, not a personal activity log. The app already has a private per-user list at `/account/saved`, so keeping `/history` global makes it a shared discovery page instead of duplicating the same concept twice.

</details>

### **F. "Why do anonymous favorite/unfavorite actions send me to login?"**

<details>
<summary><b>View explanation (Click to expand)</b></summary>

Favorites are tied to a user account. If you are not logged in, the app redirects you to `/login` and then brings you back to the object page after authentication so the action can be completed on the right account.

</details>

### **G. "Why isn't CSRF protection implemented yet?"**

<details>
<summary><b>View explanation (Click to expand)</b></summary>

This app currently uses session-cookie auth with form POSTs, but it does not yet include CSRF tokens. That is acceptable for local or portfolio use, but it should be added before any public deployment.

</details>

### **H. "How do I query the resolver programmatically?"**

<details>
<summary><b>View explanation (Click to expand)</b></summary>

Use `GET /api/resolve?q=...`. It returns the same resolution data as the HTML flow, but as JSON for scripts or other tools.

</details>

## **VII. Third-Party Data & Services**

This project depends on a few external data sources and APIs. The app uses them for lookup and display, but each service keeps its own terms, citation guidance, and usage rules.

- **NASA image backgrounds.** The animated space background pulls from NASA's public Image and Video Library. The app shows an on-page credit when a NASA image is loaded, and any reuse outside this project should follow NASA's current media usage guidance.
- **SIMBAD.** SIMBAD is the source of the primary object resolution step. If you reuse this project or publish derived results, include SIMBAD attribution or citation as required by their current data-use guidance.
- **NASA Exoplanet Archive.** Exoplanet data comes from the NASA Exoplanet Archive. If you reuse catalog output or publish derived results, follow NASA's citation guidance for the archive.
- **Gemini API.** AI summaries are generated through Google's Gemini API. Any use of that feature is subject to Google's current API terms and related policies.
