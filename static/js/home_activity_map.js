(function () {
  if (!window.homePageFilter) {
    return;
  }

  window.homePageFilter.geocodeAddress = function geocodeAddress(address) {
    const trimmedAddress = (address || "").trim();
    if (!trimmedAddress) {
      return Promise.resolve(null);
    }

    if (this.activityData.geocodeCache.has(trimmedAddress)) {
      return Promise.resolve(this.activityData.geocodeCache.get(trimmedAddress));
    }

    const geocoder = this.activityData.geocoder;
    if (!geocoder || !window.google || !window.google.maps) {
      return Promise.resolve(null);
    }

    return new Promise((resolve) => {
      geocoder.geocode({ address: trimmedAddress }, (results, status) => {
        if (status === "OK" && Array.isArray(results) && results[0] && results[0].geometry && results[0].geometry.location) {
          const location = results[0].geometry.location;
          const normalizedLocation = {
            lat: location.lat(),
            lng: location.lng(),
          };
          this.activityData.geocodeCache.set(trimmedAddress, normalizedLocation);
          resolve(normalizedLocation);
          return;
        }

        resolve(null);
      });
    });
  };

  window.homePageFilter.clearActivityMarkers = function clearActivityMarkers() {
    this.activityData.markers.forEach((marker) => marker.setMap(null));
    this.activityData.markers = [];
  };

  window.homePageFilter.updateActivityMap = async function updateActivityMap(filteredEvents) {
    const mapContainer = document.getElementById("home-activity-map");
    const mapEmptyMessage = document.getElementById("home-activity-map-empty");
    if (!mapContainer || !mapEmptyMessage) {
      return;
    }

    if (!window.google || !window.google.maps || !this.activityData.mapInstance) {
      mapEmptyMessage.style.display = "block";
      mapContainer.classList.add("is-map-unavailable");
      return;
    }

    mapEmptyMessage.style.display = "none";
    mapContainer.classList.remove("is-map-unavailable");
    this.clearActivityMarkers();

    const latestByEmployee = new Map();
    filteredEvents.forEach((event) => {
      const key = event.employee_key || "";
      if (!key || latestByEmployee.has(key)) {
        return;
      }
      latestByEmployee.set(key, event);
    });

    const markerPayloads = [];
    for (const event of latestByEmployee.values()) {
      if (!event.address) {
        continue;
      }

      const location = await this.geocodeAddress(event.address);
      if (!location) {
        continue;
      }

      markerPayloads.push({ event, location });
    }

    if (markerPayloads.length === 0) {
      const fallbackAddress = this.activityData.businessCenterAddress || "";
      const fallbackLocation = await this.geocodeAddress(fallbackAddress);
      if (fallbackLocation) {
        this.activityData.mapInstance.setCenter(fallbackLocation);
        this.activityData.mapInstance.setZoom(10);
      } else {
        this.activityData.mapInstance.setCenter({ lat: 39.8283, lng: -98.5795 });
        this.activityData.mapInstance.setZoom(4);
      }
      return;
    }

    const bounds = new window.google.maps.LatLngBounds();
    markerPayloads.forEach(({ event, location }) => {
      const marker = new window.google.maps.Marker({
        map: this.activityData.mapInstance,
        position: location,
        title: event.employee || "Employee",
        label: {
          text: (event.employee || "?").slice(0, 1).toUpperCase(),
          color: "#ffffff",
          fontWeight: "700",
        },
      });

      const directionsUrl = `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(event.address || "")}&travelmode=driving`;
      marker.addListener("click", () => {
        const content = [
          `<strong>${event.employee || "Employee"}</strong>`,
          `<div>${event.address || ""}</div>`,
          `<div style=\"margin-top:6px;\"><a href=\"${directionsUrl}\" target=\"_blank\" rel=\"noopener noreferrer\">Get directions</a></div>`,
        ].join("");
        this.activityData.infoWindow.setContent(content);
        this.activityData.infoWindow.open({
          anchor: marker,
          map: this.activityData.mapInstance,
        });
      });

      this.activityData.markers.push(marker);
      bounds.extend(location);
    });

    this.activityData.mapInstance.fitBounds(bounds, 96);
    const currentZoom = this.activityData.mapInstance.getZoom();
    if (typeof currentZoom === "number" && currentZoom > 12) {
      this.activityData.mapInstance.setZoom(12);
    }
  };

  window.initHomeActivityMap = function initHomeActivityMap() {
    if (!window.google || !window.google.maps) {
      return;
    }

    const mapContainer = document.getElementById("home-activity-map");
    if (!mapContainer) {
      return;
    }

    if (!window.homePageFilter.activityData.mapInstance) {
      window.homePageFilter.activityData.mapInstance = new window.google.maps.Map(mapContainer, {
        center: { lat: 39.8283, lng: -98.5795 },
        zoom: 4,
        mapTypeControl: false,
        fullscreenControl: true,
        streetViewControl: false,
        zoomControl: true,
        gestureHandling: "cooperative",
      });
      window.homePageFilter.activityData.geocoder = new window.google.maps.Geocoder();
      window.homePageFilter.activityData.infoWindow = new window.google.maps.InfoWindow();
    }

    window.homePageFilter.updateActivityCenter();
  };
})();
