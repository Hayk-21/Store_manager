// Pick a store's coordinates: drop a pin, or use the phone's own location.
//
// Leaflet is vendored (no CDN); the tiles come from OpenStreetMap. If tiles fail
// to load the picker still works — the marker, the inputs and the radius circle
// are all client-side, so a blank grey map is a cosmetic problem, not a broken
// form.

(function () {
  "use strict";

  // Yerevan, Republic Square — a sane place to open a map for this business.
  var FALLBACK = [40.1776, 44.5126];

  // Leaflet guesses its image folder from the script URL and gets it wrong when
  // the files are vendored rather than served from the package layout.
  if (window.L) L.Icon.Default.prototype.options.imagePath = "/static/leaflet-images/";

  function fix(value) {
    return Number(value).toFixed(6);
  }

  function setup(host) {
    var latInput = document.querySelector(host.dataset.lat);
    var lngInput = document.querySelector(host.dataset.lng);
    var radiusInput = host.dataset.radius
      ? document.querySelector(host.dataset.radius)
      : null;
    var readout = host.parentElement.querySelector("[data-readout]");
    if (!latInput || !lngInput) return;

    var hasCoords = latInput.value && lngInput.value;
    var start = hasCoords
      ? [parseFloat(latInput.value), parseFloat(lngInput.value)]
      : FALLBACK;

    var map = L.map(host, { scrollWheelZoom: false }).setView(start, hasCoords ? 17 : 13);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "© OpenStreetMap",
    }).addTo(map);

    var marker = L.marker(start, { draggable: true });
    var circle = L.circle(start, {
      radius: radiusInput ? Number(radiusInput.value) || 120 : 120,
      color: "#5b9cff",
      weight: 1,
      fillOpacity: 0.12,
    });
    if (hasCoords) {
      marker.addTo(map);
      circle.addTo(map);
    }

    function report(accuracy) {
      if (!readout) return;
      readout.textContent = hasCoords
        ? "📍 " + fix(latInput.value) + ", " + fix(lngInput.value) +
          (accuracy ? " · ճշտություն ≈" + Math.round(accuracy) + " մ" : "")
        : "Կետ նշված չէ։ Սեղմեք քարտեզին կամ «Իմ տեղը»։";
    }

    function place(latlng, accuracy) {
      latInput.value = fix(latlng.lat);
      lngInput.value = fix(latlng.lng);
      hasCoords = true;
      marker.setLatLng(latlng).addTo(map);
      circle.setLatLng(latlng).addTo(map);
      report(accuracy);
    }

    map.on("click", function (event) {
      place(event.latlng);
    });
    marker.on("dragend", function () {
      place(marker.getLatLng());
    });

    // Typing into the boxes by hand still moves the pin.
    [latInput, lngInput].forEach(function (input) {
      input.addEventListener("change", function () {
        var lat = parseFloat(latInput.value);
        var lng = parseFloat(lngInput.value);
        if (isNaN(lat) || isNaN(lng)) return;
        place({ lat: lat, lng: lng });
        map.setView([lat, lng], Math.max(map.getZoom(), 17));
      });
    });

    if (radiusInput) {
      radiusInput.addEventListener("input", function () {
        var metres = Number(radiusInput.value);
        if (metres > 0) circle.setRadius(metres);
      });
    }

    var locateButton = host.parentElement.querySelector("[data-locate]");
    if (locateButton) {
      locateButton.addEventListener("click", function () {
        if (!navigator.geolocation) {
          locateButton.textContent = "Անհասանելի";
          return;
        }
        var original = locateButton.textContent;
        locateButton.textContent = "Որոնում…";
        locateButton.disabled = true;
        navigator.geolocation.getCurrentPosition(
          function (position) {
            var point = {
              lat: position.coords.latitude,
              lng: position.coords.longitude,
            };
            place(point, position.coords.accuracy);
            map.setView([point.lat, point.lng], 18);
            locateButton.textContent = original;
            locateButton.disabled = false;
          },
          function () {
            locateButton.textContent = "Չհաջողվեց";
            locateButton.disabled = false;
            setTimeout(function () {
              locateButton.textContent = original;
            }, 4000);
          },
          { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
        );
      });
    }

    report();
    // A map created inside a collapsed <details> measures itself as zero and
    // renders grey until it is told to look again.
    var box = host.closest("details");
    if (box) {
      box.addEventListener("toggle", function () {
        if (box.open) setTimeout(function () { map.invalidateSize(); }, 60);
      });
    }
    setTimeout(function () { map.invalidateSize(); }, 200);
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-map]").forEach(setup);
  });
})();
