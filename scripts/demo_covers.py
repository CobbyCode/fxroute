# Demo catalog manifest: every cover from ~/ai/projects/fxroute-newdemopics
# (web-optimized 500px JPEGs in ~/ai/projects/fxroute-newdemopics-web),
# assigned to exactly one library. Each source file is used once.
#
# Fields per entry: (source_relpath, library, artist, album, genre, year, extra)
#   library: local | smb1 | smb2 | tidal | spotify | qobuz
#   extra: tidal quality tier (HI_RES_LOSSLESS/LOSSLESS/HIGH),
#          provider (sample_rate_hz, bit_depth), else None.
# Cover-text fidelity: artist/album come from the printed cover text.
# Covers without a printed artist are self-titled acts (artist == album),
# except where noted. One deliberate disambiguation: the tidal and the
# smb2 folder both print "Modern Legends / Hip Hop Essentials" on different
# artworks — tidal keeps "Modern Legends", smb2 uses "Hip Hop Essentials".
COVERS = [
    # ---- local library (18): curated pull from the other pools ----
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_11_51 AM (3).jpg", "local", "Mason Vale", "Copper Sky", "Americana", 2023, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_15_38 AM (5).jpg", "local", "Ruby Comet", "Electric Honey", "Funk", 2023, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 09_03_39 AM (6).jpg", "local", "Spectrum", "Spectrum", "Electronic", 2025, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 04_15_37 AM (1).jpg", "local", "Luma District", "Vector Heart", "Electronic", 2025, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 09_14_49 AM (2).jpg", "local", "Chill Vibes", "Chill Vibes", "Chillout", 2024, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 09_14_51 AM (10).jpg", "local", "Vinyl Cafe", "Vinyl Cafe", "Lo-Fi", 2024, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 09_55_26 AM (8).jpg", "local", "Static Orbit", "Static Orbit", "Electronic", 2024, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 09_55_27 AM (10).jpg", "local", "Afterglow System", "Afterglow System", "Electronic", 2025, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 10_15_56 AM (9).jpg", "local", "Summer on the Block", "Summer on the Block", "Hip-Hop", 2024, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 10_22_29 AM (1).jpg", "local", "Concrete Summer", "Concrete Summer", "Hip-Hop", 2023, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 10_39_31 AM (4).jpg", "local", "Pop Mirage", "Pop Mirage", "Pop", 2025, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 09_14_49 AM (1).jpg", "local", "Modern Legends", "Hip Hop Essentials", "Hip-Hop", 2024, None),
    ("tidal/ChatGPT Image Sep 10, 2026, 04_18_19 AM.jpg", "local", "Static Parade", "Color Radio", "Pop", 2025, None),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_55_40 AM (9).jpg", "local", "Jazz Evenings", "Jazz Evenings", "Jazz", 2024, None),
    ("tidal/ChatGPT Image Sep 10, 2026, 09_14_50 AM (4).jpg", "local", "Nature", "Nature", "Ambient", 2024, None),
    ("spotify/ChatGPT Image Sep 10, 2026, 04_11_52 AM (5).jpg", "local", "Blue Meridian", "After the Rain", "Jazz", 2024, None),
    ("qobuz/ChatGPT Image Sep 10, 2026, 09_53_49 AM (1).jpg", "local", "Neon Tides", "Neon Tides", "House", 2025, None),
    ("qobuz/ChatGPT Image Sep 10, 2026, 09_55_25 AM (5).jpg", "local", "Digital Bloom", "Digital Bloom", "Garage", 2025, None),
    # ---- SMB_Demo_Library-1 (16) ----
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_11_51 AM (1).jpg", "smb1", "Nova Static", "Midnight Relay", "Synthwave", 2025, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_15_38 AM (2).jpg", "smb1", "Saffron Tide", "Golden Static", "Indie Rock", 2023, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_15_38 AM (3).jpg", "smb1", "The Velvet Arcade", "Neon Weekend", "Synthpop", 2024, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_20_49 AM.jpg", "smb1", "Vector Isles", "Pulse Theory", "Electronic", 2025, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_21_00 AM.jpg", "smb1", "Static Coast", "Modular Hearts", "Electronic", 2024, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_22_48 AM (1).jpg", "smb1", "The Alder Quartet", "Midnight Session", "Jazz", 2023, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_22_48 AM (2).jpg", "smb1", "Iris Vale Trio", "Copper Moon", "Jazz", 2024, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_22_48 AM (3).jpg", "smb1", "June Meridian", "Smoke & Satin", "Vocal Jazz", 2023, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_22_48 AM (4).jpg", "smb1", "Velvet Brass Union", "After Hours Mosaic", "Jazz", 2024, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_22_48 AM (5).jpg", "smb1", "Harbor Swing", "Blue Avenue", "Jazz", 2023, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_24_59 AM (1).jpg", "smb1", "Nocturne Harbor", "Golden Tides", "Ambient", 2024, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 04_24_59 AM (3).jpg", "smb1", "Aural Vector", "Prism Engine", "Electronic", 2026, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 09_14_50 AM (5).jpg", "smb1", "Voices", "Voices", "Soul", 2024, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 10_15_55 AM (5).jpg", "smb1", "Street Poet", "Street Poet", "Hip-Hop", 2024, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 10_15_56 AM (6).jpg", "smb1", "Subway Saints", "Subway Saints", "Hip-Hop", 2023, None),
    ("smb-library-1/ChatGPT Image Sep 10, 2026, 10_55_39 AM (4).jpg", "smb1", "Chill House", "Chill House", "House", 2025, None),
    # ---- SMB_Demo_Library-2 (12) ----
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 04_20_33 AM.jpg", "smb2", "Neon Vale", "Drift Circuit", "Electronic", 2024, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 09_55_24 AM (2).jpg", "smb2", "Circuit Dreams", "Circuit Dreams", "Synthwave", 2025, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 09_55_26 AM (6).jpg", "smb2", "Night Protocol", "Night Protocol", "Electronic", 2024, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 09_55_27 AM (9).jpg", "smb2", "Prism Engine", "Progressive Trance", "Electronic", 2025, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 10_15_54 AM (2).jpg", "smb2", "Sunset Lowrider", "Sunset Lowrider", "Hip-Hop", 2023, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 10_15_56 AM (8).jpg", "smb2", "Concrete Crown", "Concrete Crown", "Hip-Hop", 2024, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 10_22_30 AM (2).jpg", "smb2", "Empire Echoes", "Empire Echoes", "Hip-Hop", 2023, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 10_22_30 AM (4).jpg", "smb2", "Cipher District", "Cipher District", "Hip-Hop", 2024, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 10_22_30 AM (5).jpg", "smb2", "Tape Royalty", "Tape Royalty", "Hip-Hop", 2022, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 10_22_31 AM (8).jpg", "smb2", "Blue Note Alley", "Blue Note Alley", "Hip-Hop", 2024, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 10_39_30 AM (1).jpg", "smb2", "Neon Hearts", "Neon Hearts", "Pop", 2024, None),
    ("smb-library-2/ChatGPT Image Sep 10, 2026, 10_55_39 AM (3).jpg", "smb2", "Late Afternoon Grooves", "Late Afternoon Grooves", "Jazz", 2024, None),
    # ---- TIDAL (18) ----
    ("tidal/ChatGPT Image Sep 10, 2026, 04_24_59 AM (2).jpg", "tidal", "The Marlowe Ensemble", "Velvet Skyline", "Jazz", 2025, "HI_RES_LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 09_03_39 AM (9).jpg", "tidal", "Blue Note", "Jazz Classics", "Jazz", 2023, "LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 09_14_50 AM (6).jpg", "tidal", "Neon Pulse", "Neon Pulse", "Synthwave", 2024, "LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 09_14_50 AM (8).jpg", "tidal", "Midnight Drive", "Midnight Drive", "Electronic", 2023, "LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 09_23_00 AM (1).jpg", "tidal", "Modern Legends", "Modern Legends", "Hip-Hop", 2024, "LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 09_55_24 AM (3).jpg", "tidal", "Pulse Array", "Pulse Array", "Electronic", 2025, "HIGH"),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_15_54 AM (3).jpg", "tidal", "Midnight Cipher", "Midnight Cipher", "Hip-Hop", 2023, "HI_RES_LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_15_54 AM (4).jpg", "tidal", "Golden Tape", "Golden Tape", "Hip-Hop", 2022, "LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_15_56 AM (7).jpg", "tidal", "Rooftop Rhymes", "Rooftop Rhymes", "Hip-Hop", 2023, "HI_RES_LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_15_57 AM (10).jpg", "tidal", "After Hours Player", "After Hours Player", "Hip-Hop", 2022, "LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_22_31 AM (9).jpg", "tidal", "Corner Lessons", "Corner Lessons", "Hip-Hop", 2024, "LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_39_30 AM (2).jpg", "tidal", "Electric Avenue", "Electric Avenue", "Synthpop", 2024, "HI_RES_LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_39_30 AM (3).jpg", "tidal", "Cassette Kisses", "Cassette Kisses", "Pop", 2023, "LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_39_31 AM (5).jpg", "tidal", "Velvet Satellite", "Velvet Satellite", "Pop", 2025, "HI_RES_LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_39_31 AM (7).jpg", "tidal", "Flashback Fever", "Flashback Fever", "Pop", 2024, "HIGH"),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_39_33 AM (9).jpg", "tidal", "Starlight Boulevard", "Starlight Boulevard", "Pop", 2025, "HI_RES_LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_55_39 AM (6).jpg", "tidal", "Lazy Sundays", "Lazy Sundays", "Soul", 2024, "LOSSLESS"),
    ("tidal/ChatGPT Image Sep 10, 2026, 10_55_39 AM (7).jpg", "tidal", "Late Night Grooves", "Late Night Grooves", "Soul", 2023, "HIGH"),
    # ---- Spotify (5): small queue, ~3 tracks per album ----
    ("spotify/ChatGPT Image Sep 10, 2026, 09_03_38 AM (4).jpg", "spotify", "Various Artists", "Analog Stories", "Folk", 2023, (44100, None)),
    ("spotify/ChatGPT Image Sep 10, 2026, 09_03_38 AM (5).jpg", "spotify", "Acoustic Seaside", "Acoustic Seaside", "Chillout", 2024, (44100, None)),
    ("spotify/ChatGPT Image Sep 10, 2026, 09_03_39 AM (7).jpg", "spotify", "Luna", "Luna", "Pop", 2024, (48000, None)),
    ("spotify/ChatGPT Image Sep 10, 2026, 09_46_52 AM (1).jpg", "spotify", "Late Afternoon", "Late Afternoon", "Jazz", 2023, (44100, None)),
    ("spotify/ChatGPT Image Sep 10, 2026, 10_22_32 AM (10).jpg", "spotify", "Noir Dynasty", "Noir Dynasty", "Hip-Hop", 2024, (48000, None)),
    # ---- Qobuz (6): FLAC tiers carry bit depth like the real payload ----
    ("qobuz/ChatGPT Image Sep 10, 2026, 09_03_38 AM (2).jpg", "qobuz", "Jazz Meets Soul", "Jazz Meets Soul", "Jazz", 2024, (96000, 24)),
    ("qobuz/ChatGPT Image Sep 10, 2026, 10_22_30 AM (3).jpg", "qobuz", "Velvet Lowrider", "Velvet Lowrider", "Hip-Hop", 2023, (44100, 16)),
    ("qobuz/ChatGPT Image Sep 10, 2026, 10_22_31 AM (6).jpg", "qobuz", "Bronx Monarchy", "Bronx Monarchy", "Hip-Hop", 2022, (96000, 24)),
    ("qobuz/ChatGPT Image Sep 10, 2026, 10_39_31 AM (6).jpg", "qobuz", "Laser Youth", "Laser Youth", "Synthpop", 2025, (96000, 24)),
    ("qobuz/ChatGPT Image Sep 10, 2026, 10_39_32 AM (8).jpg", "qobuz", "Pixel Paradise", "Pixel Paradise", "Synthpop", 2024, (44100, 16)),
    ("qobuz/ChatGPT Image Sep 10, 2026, 10_55_41 AM (10).jpg", "qobuz", "Good Music", "Good Music", "Soul", 2024, (48000, 24)),
]

# Track list printed on the Smoke & Satin cover; honored for the first six
# tracks so the library matches the artwork.
SMOKE_AND_SATIN_TRACKS = [
    "A Little Later",
    "Smoke & Satin",
    "The Way You Stay",
    "Midnight in Between",
    "Soft Enough",
    "Another Tomorrow",
]

LIB_PREFIX = {"local": "loc", "smb1": "s1", "smb2": "s2", "tidal": "t", "spotify": "sp", "qobuz": "qz"}
