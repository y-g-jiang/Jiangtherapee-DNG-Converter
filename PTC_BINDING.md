# PTC binding

The tools/ptc.py module binds a recorded camera and ISO to the bundled ref/ptc_model.csv and models.csv. The JDN integration calls bind_camera and uses RAW headers only; no broad RN/FWC search is required.

The source PTC axis is 0..255: normalized shot slope is b_shot / 255, normalized read variance is a_read_var / 255^2, and effective saturation electrons are 255 / b_shot. These coefficients must not be treated as native 12/14/16-bit RAW DN. JDN retains the matched dark frame for read noise, actual LibRaw black/white for normalization, and its existing model contract without enabling catalog PRNU.

Automatic binding requires a known camera; fuzzy name/sensor substitution is disabled. Exact recorded ISO is preferred. Missing ISO is interpolated only between recorded values for that same camera, using the existing log-ISO/log-coefficient rule; no out-of-range extrapolation is used. Manual FWC and explicitly selected proxies remain available. S5II is not in this table and is not silently substituted with S5.

PTC tables are package-relative. JDC_PTC_TABLE and JDC_PTC_MODELS can override their locations. The independent ptc_z8.csv normalized representation was used locally as a numeric cross-check; it is not required at runtime.

