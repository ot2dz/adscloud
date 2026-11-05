"use strict";

// The hosted ADRA Cloud this build always talks to. Baked in so end users never
// type or pick a URL; to point the extension at another deployment, change this
// value and ship a new build.
const ADRA_DEFAULT_CLOUD_URL = "https://ads.botdz.com";

// Server rule mirrored locally so the signup form can validate before sending.
const ADRA_MIN_PASSWORD_LEN = 10;
