(() => {
  "use strict";

  const form = document.querySelector("#branding-form");
  if (!form) return;

  let dirty = false;
  const dirtyNotice = document.querySelector("#branding-dirty");
  const markDirty = () => {
    dirty = true;
    dirtyNotice.hidden = false;
  };

  const selectedValue = (name, fallback = "") =>
    form.querySelector(`[name="${name}"]:checked`)?.value ||
    form.querySelector(`[name="${name}"]`)?.value ||
    fallback;

  const syncColor = (picker, text) => {
    picker.addEventListener("input", () => {
      text.value = picker.value.toUpperCase();
      text.dispatchEvent(new Event("input", { bubbles: true }));
    });
    text.addEventListener("input", () => {
      if (/^#[0-9a-f]{6}$/i.test(text.value)) picker.value = text.value;
    });
  };

  document.querySelectorAll("[data-color-picker]").forEach((picker) => {
    const text = document.querySelector(`#${picker.dataset.colorPicker}`);
    if (text) syncColor(picker, text);
  });

  const bindRepeatRow = (row) => {
    const picker = row.querySelector('input[type="color"]');
    const text = row.querySelector('input:not([type="color"])');
    if (picker && text) syncColor(picker, text);
    row.querySelector(".remove-row")?.addEventListener("click", () => {
      row.remove();
      markDirty();
      updatePreview();
    });
  };
  document.querySelectorAll(".repeat-row").forEach(bindRepeatRow);
  document.querySelectorAll("[data-add-color]").forEach((button) => {
    button.addEventListener("click", () => {
      const name = button.dataset.addColor;
      const list = document.querySelector(`[data-repeat-list="${name}"]`);
      const row = document.createElement("div");
      row.className = "repeat-row";
      row.innerHTML = `<input type="color" value="#64748B" aria-label="Farbe auswählen"><input name="${name}" value="#64748B" pattern="#[0-9A-Fa-f]{6}"><button type="button" class="remove-row">Entfernen</button>`;
      list.append(row);
      bindRepeatRow(row);
      markDirty();
      updatePreview();
    });
  });

  const normalizeTag = (name, value) => {
    value = value.trim();
    if (name === "hashtags") value = `#${value.replace(/^#+/, "").replace(/\s+/g, "")}`;
    if (name === "mentions") value = `@${value.replace(/^@+/, "").toLowerCase()}`;
    return value;
  };
  const bindTag = (tag) => {
    tag.querySelector("button")?.addEventListener("click", () => {
      tag.remove();
      markDirty();
    });
  };
  document.querySelectorAll(".tag").forEach(bindTag);
  document.querySelectorAll("[data-tag-editor]").forEach((editor) => {
    const name = editor.dataset.tagEditor;
    const entry = editor.querySelector(".tag-entry input");
    const add = editor.querySelector(".tag-entry button");
    const insert = () => {
      const value = normalizeTag(name, entry.value);
      if (!value || ["#", "@"].includes(value)) return;
      const duplicate = [...editor.querySelectorAll(`input[name="${name}"]`)].some(
        (input) => input.value.toLowerCase() === value.toLowerCase(),
      );
      if (duplicate) {
        entry.setCustomValidity("Dieser Eintrag ist bereits vorhanden.");
        entry.reportValidity();
        return;
      }
      entry.setCustomValidity("");
      const tag = document.createElement("span");
      tag.className = "tag";
      const label = document.createElement("span");
      label.textContent = value;
      const hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = name;
      hidden.value = value;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.setAttribute("aria-label", `${value} entfernen`);
      remove.textContent = "×";
      tag.append(label, hidden, remove);
      editor.querySelector(".tag-items").append(tag);
      bindTag(tag);
      entry.value = "";
      markDirty();
    };
    add.addEventListener("click", insert);
    entry.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        insert();
      }
    });
  });

  const serializeTeams = () => {
    const teams = [...document.querySelectorAll(".team-naming-row")].map((row) => ({
      team_id: row.dataset.teamId,
      display_name: row.querySelector("[data-team-display]").value.trim(),
      short_name: row.querySelector("[data-team-short]").value.trim(),
      active: row.querySelector("[data-team-active]").checked,
    }));
    document.querySelector("#team-names-json").value = JSON.stringify(teams);
  };
  document.querySelectorAll(".team-naming-row input").forEach((input) => {
    input.addEventListener("input", () => {
      const row = input.closest(".team-naming-row");
      const display = row.querySelector("[data-team-display]").value.trim();
      const short = row.querySelector("[data-team-short]").value.trim();
      const preview = row.querySelector(".team-name-preview");
      const strong = document.createElement("strong");
      strong.textContent = display;
      preview.replaceChildren(
        document.createTextNode("Vorschau: "),
        strong,
        document.createTextNode(short ? ` (${short})` : ""),
      );
      serializeTeams();
      updateExamples();
    });
  });

  const sponsorData = (row) => {
    const flags = {};
    row.querySelectorAll("[data-sponsor-flag]").forEach((input) => {
      flags[input.dataset.sponsorFlag] = input.checked;
    });
    return {
      name: row.querySelector("[data-sponsor-name]").value.trim(),
      media_asset_id: row.querySelector("[data-sponsor-media]").value,
      instagram_mention: row.querySelector("[data-sponsor-mention]").value.trim(),
      placement: row.querySelector("[data-sponsor-placement]").value,
      team_ids: [...row.querySelectorAll("[data-sponsor-team]:checked")].map((x) => x.value),
      valid_from: row.querySelector("[data-sponsor-valid-from]").value,
      valid_until: row.querySelector("[data-sponsor-valid-until]").value,
      ...flags,
    };
  };
  const serializeSponsors = () => {
    const sponsors = [...document.querySelectorAll("#sponsor-list .sponsor-row")]
      .map(sponsorData)
      .filter((item) => item.name);
    document.querySelector("#sponsors-json").value = JSON.stringify(sponsors);
  };
  const bindSponsor = (row) => {
    row.querySelector(".remove-sponsor")?.addEventListener("click", () => {
      row.remove();
      serializeSponsors();
      markDirty();
      updatePreview();
    });
    row.querySelectorAll("input,select").forEach((input) =>
      input.addEventListener("change", () => {
        serializeSponsors();
        updatePreview();
      }),
    );
  };
  document.querySelectorAll("#sponsor-list .sponsor-row").forEach(bindSponsor);
  document.querySelector("#add-sponsor")?.addEventListener("click", () => {
    const row = document.querySelector("#sponsor-template").content.firstElementChild.cloneNode(true);
    document.querySelector("#sponsor-list").append(row);
    bindSponsor(row);
    markDirty();
  });

  const clubName = form.dataset.clubName.trim() || "Dein Verein";
  const clubShortName = form.dataset.clubShortName.trim();
  const currentTeam = () =>
    document.querySelector(".team-naming-row [data-team-display]")?.value.trim() ||
    clubShortName ||
    clubName;
  const venueName = () =>
    form.querySelector('[name="home_venue_short"]')?.value.trim() ||
    document.querySelector("#home-venue")?.value.trim() ||
    "eurer Heimspielstätte";
  const examples = {
    tone: {
      factual: () => `Am Sonntag empfängt ${currentTeam()} den kommenden Gegner in ${venueName()}.`,
      emotional: () => `Heimspielzeit für ${currentTeam()} – gemeinsam alles geben!`,
      motivating: () => `Unterstützt ${currentTeam()} am Sonntag und macht ${venueName()} zur Festung!`,
      casual: () => `Sonntag, Heimspiel, ${venueName()}. Wir sehen uns!`,
      professional: () => `${currentTeam()} freut sich auf die nächste Begegnung in ${venueName()}.`,
      traditional: () => `Gemeinsam für ${clubName}: Am Sonntag zählt jede Stimme in ${venueName()}.`,
    },
    address: {
      du: () => `Komm vorbei und unterstütze ${currentTeam()}.`,
      ihr: () => `Kommt vorbei und unterstützt ${currentTeam()}.`,
      neutral: () => `Unterstützung für ${currentTeam()} ist herzlich willkommen.`,
    },
    length: {
      short: () => `Heimspiel für ${currentTeam()} in ${venueName()}.`,
      medium: () => `Am Sonntag steht für ${currentTeam()} das nächste Heimspiel in ${venueName()} an.`,
      detailed: () => `Am Sonntag bestreitet ${currentTeam()} das nächste Heimspiel in ${venueName()}. Alle Vereinsmitglieder und Fans sind herzlich eingeladen, die Mannschaft zu unterstützen.`,
    },
    call_to_action: {
      support: () => `Unterstützt ${currentTeam()} vor Ort!`,
      share: () => "Teilt den Beitrag mit euren Freunden!",
      comment: () => "Schreibt euren Tipp in die Kommentare!",
      attend: () => `Kommt zum Spiel nach ${venueName()}!`,
      none: () => "Keine Handlungsaufforderung",
      custom: () => form.querySelector('[name="cta_custom"]').value.trim() || "Eigene Handlungsaufforderung",
    },
  };
  const updateExamples = () => {
    document.querySelectorAll("[data-example-select]").forEach((select) => {
      const kind = select.dataset.exampleSelect;
      const factory = examples[kind]?.[select.value];
      const target = document.querySelector(`[data-example="${kind}"]`);
      if (factory && target) target.textContent = factory();
    });
  };

  const setFont = (select, sample, variable) => {
    const option = select.selectedOptions[0];
    if (option?.dataset.url) {
      const family = `ClubFont-${option.value.replace(/[^a-z0-9]/gi, "")}`;
      const style = document.createElement("style");
      style.dataset.brandFont = variable;
      document.querySelector(`style[data-brand-font="${variable}"]`)?.remove();
      style.textContent = `@font-face{font-family:"${family}";src:url("${option.dataset.url}") format("${option.dataset.format || "woff2"}");font-display:swap}`;
      document.head.append(style);
      sample.style.fontFamily = family;
      document.querySelector("#branding-preview").style.setProperty(variable, family);
    } else {
      const family = option?.dataset.family || "system-ui";
      document.querySelector(`style[data-brand-font="${variable}"]`)?.remove();
      sample.style.fontFamily = family;
      document.querySelector("#branding-preview").style.setProperty(variable, family);
    }
  };

  const updatePreview = () => {
    const preview = document.querySelector("#branding-preview");
    const primary = form.querySelector('[name="primary_color"]').value;
    const secondary = form.querySelector('[name="secondary_color"]').value;
    const accent = form.querySelector('[name="accent_colors"]')?.value || primary;
    preview.style.setProperty("--brand-primary", primary);
    preview.style.setProperty("--brand-secondary", secondary);
    preview.style.setProperty("--brand-accent", accent);
    preview.dataset.logoPosition = selectedValue("logo_placement", "top-left");
    preview.dataset.playerPosition = selectedValue("player_position", "center-right");
    preview.dataset.safeMargins = selectedValue("safe_margins", "normal");
    preview.dataset.textAlignment = selectedValue("text_alignment", "left");
    preview.dataset.background = selectedValue("background_style", "gradient");
    preview.dataset.style = selectedValue("graphic_style", "modern");
    preview.dataset.textAmount = selectedValue("image_text_amount", "normal");
    preview.style.setProperty("--player-ratio", `${selectedValue("player_background_ratio", 60)}%`);
    const format = selectedValue("preview_format", "feed");
    preview.classList.toggle("preview-story", format === "story");
    preview.classList.toggle("preview-feed", format !== "story");
    const type = selectedValue("preview_type", "announcement");
    preview.querySelector(".preview-kicker").textContent = type === "result" ? "ERGEBNIS" : "SPIELANKÜNDIGUNG";
    preview.querySelector(".preview-versus").textContent = type === "result" ? "3 : 1" : "VS. NÄCHSTER GEGNER";
    preview.querySelector(".preview-club").textContent = currentTeam();
    preview.querySelector(".preview-details").textContent = `SONNTAG · ${venueName()}`;
    preview.querySelector(".preview-sponsor").hidden = !document.querySelector("#sponsor-list .sponsor-row");
    const logoOption = document.querySelector("#club-logo").selectedOptions[0];
    const logoBox = preview.querySelector(".preview-logo");
    logoBox.innerHTML = "";
    if (logoOption?.dataset.preview) {
      const img = document.createElement("img");
      img.src = logoOption.dataset.preview;
      img.alt = "Vereinslogo in der Vorschau";
      logoBox.append(img);
    } else {
      const span = document.createElement("span");
      span.textContent = "Vereinslogo";
      logoBox.append(span);
    }
    setFont(document.querySelector("#primary-font"), document.querySelector("#primary-font-sample"), "--brand-font-primary");
    setFont(document.querySelector("#secondary-font"), document.querySelector("#secondary-font-sample"), "--brand-font-secondary");
    document.querySelector('input[name="player_background_ratio"]')?.nextElementSibling?.querySelector("output")?.replaceChildren(`${selectedValue("player_background_ratio", 60)} % Spieler`);
    updateExamples();
  };

  form.addEventListener("input", (event) => {
    if (!event.target.closest(".preview-toolbar")) markDirty();
    updatePreview();
  });
  form.addEventListener("change", (event) => {
    if (!event.target.closest(".preview-toolbar")) markDirty();
    updatePreview();
  });
  form.addEventListener("submit", () => {
    serializeTeams();
    serializeSponsors();
    dirty = false;
  });
  document.querySelector("#refresh-preview")?.addEventListener("click", updatePreview);
  document.querySelector("#branding-discard")?.addEventListener("click", () => {
    if (!dirty || window.confirm("Ungespeicherte Änderungen wirklich verwerfen?")) window.location.reload();
  });
  document.querySelector("#branding-reset")?.addEventListener("click", (event) => {
    if (!window.confirm("Branding wirklich auf sichere Standardwerte zurücksetzen? Übernommene Altwerte bleiben erhalten.")) event.preventDefault();
  });
  window.addEventListener("beforeunload", (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  serializeTeams();
  serializeSponsors();
  updatePreview();
})();
