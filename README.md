# **astronomy-multi-catalog-cross-matcher**

## **I. App Description**

An FastAPI WebApp that takes an unstructured, informally-typed star name, resolves it deterministically across independent astronomical catalogs via a cross-identification pipeline, and then—as a distinct, separate step—translates the resulting structured data into a plain-language explanation a non-specialist can actually read.

**Architectural principle:** the resolution engine (deterministic, SQL/ADQL-driven) and the narrative layer (AI-generated) are kept strictly separate — the AI layer can never affect matching logic or introduce a factual error the SQL layer didn't already contain.

## **II. App & User Workflow**

User types in an informal, human-typed star name ("51 Peg", "HD 217014", even slightly messy input), and it:

1. **Normalises** the query (fixes whitespace/casing, canonicalizes catalog prefixes like HD/HIP/GJ/TYC without stripping them)
2. **[Resolves](#a-resolve-explanation)** identity against SIMBAD (the standard astronomical object database) via a TAP/ADQL query, pulling the canonical name, coordinates, spectral type, and every known alias in one round trip
3. **Cross-matches** those aliases against the NASA Exoplanet Archive to find any known planets orbiting that star
4. **Classifies** the result into one of [four explicit states](#b-four-states-explanation) — `RESOLVED`, `PARTIAL`, `AMBIGUOUS`, `UNRESOLVED` — rather than quietly picking one answer or silently failing.
5. **Caches** the result with a 14-day TTL (1 hour for failed/ambiguous lookups, so bugs self-heal quickly)
6. **Generates** a plain-English AI narrative (via Gemini API) explaining the result for a non-specialist, kept strictly separate from and never able to corrupt the underlying scientific data

## **III. Real-world examples**

Try these searches once you verify the app works against the real APIs:

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


## **IV. Further Explanation**

### **A. Resolve Explanation**

**"Resolve"** = take an informal, ambiguous star name and turn it into a confirmed astronomical identity. The app is answering the question: "**Do we know what object the user is asking about, and can we find it across multiple independent catalogs?**"

Example: the user types `"51 peg"` (lowercase, casual spelling of a real star). The app resolves this by:

1. Normalising it to `"51 Peg"` (correct casing)
2. Asking SIMBAD: "Do you know an object called '51 Peg'?" → yes, it's `"51 Pegasi"` (canonical name)
3. Getting back aliases: `["HD 217014", "HIP 113357", "51 Peg", "51 Pegasi", ...]`
4. Asking NASA Exoplanet Archive: "Do any of these aliases host known planets?" → yes, `"HD 217014"` does
5. Getting back planet data: `51 Peg b` (an exoplanet, discovered 1995)

At each step, gains more certainty about what the user was asking for. "Resolve" is successfully completing that chain.

### **B. Four States Explanation**

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

#### **Decision Tree**

```text
Does SIMBAD know this object?
├─ NO  → UNRESOLVED (end)
└─ YES, but is it ambiguous?
   ├─ YES (multiple candidates) → AMBIGUOUS (show list, end)
   └─ NO (one object)
      └─ Does Exoplanet Archive have planets for this object?
         ├─ YES → RESOLVED (show full profile)
         └─ NO  → PARTIAL (show star data, note no planets)
```

**Each state is mutually exclusive and exhaustive** — every possible search outcome falls into exactly one bucket.
