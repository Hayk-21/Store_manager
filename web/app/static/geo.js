// "Use my location" — fills a latitude/longitude pair from the browser.
// Standing in the shop doorway and pressing this is the least error-prone way to
// get coordinates that the geofence will actually agree with.
document.addEventListener("click", function (event) {
  const button = event.target.closest("[data-geo]");
  if (!button) return;

  const [latSel, lngSel] = button.dataset.geo.split(",");
  const latInput = document.querySelector(latSel.trim());
  const lngInput = document.querySelector(lngSel.trim());
  if (!latInput || !lngInput) return;

  if (!navigator.geolocation) {
    button.textContent = "Անհասանելի";
    return;
  }

  const original = button.textContent;
  button.textContent = "Որոնում…";
  button.disabled = true;

  navigator.geolocation.getCurrentPosition(
    function (position) {
      latInput.value = position.coords.latitude.toFixed(6);
      lngInput.value = position.coords.longitude.toFixed(6);
      button.textContent = Math.round(position.coords.accuracy) + " մ ճշտություն";
      button.disabled = false;
      setTimeout(function () { button.textContent = original; }, 4000);
    },
    function () {
      button.textContent = "Չհաջողվեց";
      button.disabled = false;
      setTimeout(function () { button.textContent = original; }, 4000);
    },
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
  );
});
