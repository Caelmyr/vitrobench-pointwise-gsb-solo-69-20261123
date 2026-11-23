import {badge, btn, e, img, n, stat, table} from "./widgets.js";

// Cutting-plan workspace. All geometry is computed server-side from a frozen
// Glass pane census; this module only presents the two layouts, adoption and
// the original-photo point lookup.

function censusChoices(results) {
  return results.filter(item => item.kind === "panes");
}

function statusBadge(plan) {
  if (plan.status === "adopted") return badge("Adopted · frozen", "ready");
  if (plan.status === "retired") return badge("Retired", "caution");
  return badge("Draft", "");
}

export function renderSection(tile, plans, results) {
  const censuses = censusChoices(results || []);
  const adopted = plans.find(plan => plan.status === "adopted");
  const list = plans.length
    ? `<div class="divider-list scroll-list">${plans.map(plan => `
      <div class="row">
        <div class="grow"><strong class="row-title">${e(plan.title)}</strong><br>
          <small>${e(plan.strategy === "economy" ? "Economy (fewest sheets)" : "Neighbour grouping")} ·
          ${plan.sheet_count} sheet(s) · ${plan.layout.length} pane(s) · v${plan.version}</small></div>
        ${statusBadge(plan)}
        ${btn("Open", "cut-open", "tiny secondary", null, `data-id="${plan.id}"`)}
      </div>`).join("")}</div>`
    : "<p class='soft'>No cutting plans yet. Run a Glass pane census, then lay out the confirmed panes.</p>";

  return `<section class="card stack" id="cutting-workspace">
    <div class="title-row"><h3>Replacement cutting plans</h3>
      ${censuses.length ? btn("New cutting plan", "cut-new", "primary", "plus") : ""}</div>
    ${censuses.length
      ? `<p class="soft">Source census: ${censuses.length} saved run(s). Only panes with a fully confirmed edge are cut; panes reaching an unphotographed edge stay blank.</p>`
      : "<p class='soft'>Run and save a Glass pane census in the measurement form first.</p>"}
    ${adopted ? `<p class="notice">Adopted plan “${e(adopted.title)}” is the version sent to the workshop and cannot be moved. Retire it before adopting another.</p>` : ""}
    ${list}
  </section>`;
}

export function newPlanDialog(results, plans, fullCensuses) {
  const censuses = censusChoices(results);
  if (!censuses.length) throw Error("Save a Glass pane census before creating a cutting plan.");
  const options = censuses.map(item => {
    const bounded = item.metrics.bounded;
    return `<option value="${item.id}">${e(item.label)} · ${bounded} bounded · v${item.source_version}</option>`;
  }).join("");
  const grainBlocks = censuses.map(item => {
    const full = fullCensuses[item.id];
    const bounded = full ? full.metrics.rows.filter(row => row.state === "bounded") : [];
    const inputs = bounded.map(row => `
      <label class="grain-cell"><span>Pane ${row.pane}</span>
        <input type="number" name="grain-${row.pane}" min="0" max="179.999" step="1" value="0"
          data-census="${item.id}"></label>`).join("");
    return `<fieldset class="grain-grid" data-grain-for="${item.id}">
      <legend>Grain direction (degrees clockwise from the photo x-axis) · ${bounded.length} bounded panes</legend>
      ${bounded.length ? inputs : "<p class='soft'>This census has no fully bounded panes.</p>"}
    </fieldset>`;
  }).join("");
  return {
    title: "Lay out replacement cuts",
    body: `
      <div class="form-columns">
        <label class="field"><span>Source Glass pane census</span>
          <select name="census_result_id" id="cut-census-select">${options}</select></label>
        <label class="field"><span>Plan title</span><input name="title" maxlength="115" value="Replacement cut ${new Date().toLocaleDateString()}" required></label>
      </div>
      <div class="form-columns">
        <label class="field"><span>Layout strategy</span><select name="strategy">
          <option value="economy">Economy · fewest whole stock sheets</option>
          <option value="neighbor">Neighbour · keep original adjacent panes together</option>
        </select></label>
      </div>
      <div class="form-columns">
        <label class="field"><span>Stock sheet width (mm)</span><input name="sheet_width_mm" type="number" min="10" max="10000" step="1" value="600" required></label>
        <label class="field"><span>Stock sheet height (mm)</span><input name="sheet_height_mm" type="number" min="10" max="10000" step="1" value="400" required></label>
        <label class="field"><span>Saw kerf / cut gap (mm)</span><input name="kerf_mm" type="number" min="0" max="200" step="0.5" value="2" required></label>
        <label class="field"><span>Edge margin (mm)</span><input name="margin_mm" type="number" min="0" max="1000" step="1" value="10" required></label>
      </div>
      ${grainBlocks}
      <p class="soft">Only fully bounded panes are cut; panes reaching an unphotographed edge stay blank. A pane may be turned 180° but is never flipped.</p>`,
  };
}

function grainTable(plan) {
  if (!plan.pieces.length) return "";
  return `<details><summary>Grain direction per pane (${plan.pieces.length})</summary>
    <div class="table-wrap"><table><thead><tr><th>Pane</th><th>Grain angle (°)</th><th>Cut box (mm)</th><th>On sheet</th></tr></thead><tbody>
    ${plan.pieces.map(piece => {
      const place = plan.layout.find(p => p.pane === piece.pane);
      return `<tr><td>${piece.pane}</td><td>${n(piece.angle, 2)}</td>
        <td>${n(piece.width)} × ${n(piece.height)}</td>
        <td>#${place.sheet}${place.flipped ? " · 180°" : ""}</td></tr>`;
    }).join("")}
    </tbody></table></div></details>`;
}

function excludedTable(plan) {
  if (!plan.excluded.length) return "";
  return `<details><summary>Held-back panes (${plan.excluded.length})</summary>
    ${table(["Pane", "Reason"], plan.excluded.map(item => [item.pane, e(item.reason)]))}</details>`;
}

function groupTable(plan) {
  if (plan.strategy !== "neighbor" || !plan.adjacency_groups.length) return "";
  return `<details><summary>Original-window neighbour groups (${plan.adjacency_groups.length})</summary>
    ${table(["Group", "Panes kept on one sheet"], plan.adjacency_groups
      .map((group, i) => [i + 1, group.join(", ")]))}</details>`;
}

export function planCard(plan) {
  const base = `/api/cutting-plans/${plan.id}`;
  const frozen = plan.status === "adopted" || plan.status === "retired";
  const diagrams = Array.from({length: plan.sheet_count}, (_, i) => i + 1)
    .map(number => `<figure class="stage">${img(`${base}/sheet/${number}.png?v=${plan.version}`, `Stock sheet ${number}`)}
      <figcaption class="stage-foot">Stock sheet ${number} of ${plan.sheet_count}</figcaption></figure>`).join("");
  const canAdopt = plan.status === "draft";
  return `<section class="card stack">
    <div class="title-row"><div><h3>${e(plan.title)}</h3>
      <small>${e(plan.strategy === "economy" ? "Economy layout" : "Neighbour layout")} · v${plan.version}</small></div>
      ${statusBadge(plan)}</div>
    <p class="notice">${e(plan.strategy_note)}</p>
    <div class="metrics">
      ${stat("Stock sheets", plan.sheet_count)}
      ${stat("Panes cut", plan.layout.length)}
      ${stat("Held back", plan.excluded.length)}
      ${stat("Sheet size (mm)", `${n(plan.spec.sheet_width_mm)} × ${n(plan.spec.sheet_height_mm)}`)}
      ${stat("Kerf / margin (mm)", `${n(plan.spec.kerf_mm)} / ${n(plan.spec.margin_mm)}`)}
    </div>
    ${diagrams}
    <figure class="stage">${img(`${base}/source.png?v=${plan.version}`, "Confirmed pane edges on the original registered photograph")}
      <figcaption class="stage-foot">Original registered photo · teal edges are cut, amber are held back</figcaption></figure>
    ${grainTable(plan)}${groupTable(plan)}${excludedTable(plan)}
    <div class="actions">
      ${canAdopt ? btn("Adopt this plan for the workshop", "cut-adopt", "primary", "check", `data-id="${plan.id}"`) : ""}
      ${plan.status === "adopted" ? btn("Retire adopted plan", "cut-retire", "danger", null, `data-id="${plan.id}"`) : ""}
      ${btn("Locate a point from the original photo", "cut-locate", "secondary", "measure", `data-id="${plan.id}"`)}
      ${btn("Back to cutting plans", "cut-back", "ghost")}
    </div>
    ${plan.queries && plan.queries.length ? queryHistory(plan) : ""}
  </section>`;
}

function queryHistory(plan) {
  return `<details open><summary>Recorded edge-point lookups (${plan.queries.length})</summary>
    ${table(["When", "Pane", "Sheet", "From left (mm)", "From top (mm)", "180°"],
      plan.queries.map(q => [new Date(q.recorded_at).toLocaleString(), q.pane, q.sheet,
        n(q.distance_from_left_mm), n(q.distance_from_top_mm), q.turned_180 ? "yes" : "no"]))}</details>`;
}

export function locateDialog(plan) {
  const panes = plan.pieces.map(piece => piece.pane);
  const base = `/api/cutting-plans/${plan.id}`;
  return {
    title: `Locate an original-photo point · ${plan.title}`,
    body: `
      <p class="notice">Click a point on a pane’s visible edge (or interior) in the original photo. It must lie inside that pane’s confirmed outline. The reply gives the stock sheet and its distance from both sheet edges.</p>
      <div class="form-columns">
        <label class="field"><span>Saved pane number</span>
          <select name="pane">${panes.map(p => `<option value="${p}">Pane ${p}</option>`).join("")}</select></label>
      </div>
      <div class="stage cutting-pick" id="cut-pick-stage">
        ${img(`${base}/source.png?v=${plan.version}`, "Original photo with confirmed pane edges")}
      </div>
      <p class="soft" id="cut-pick-hint">Click inside the highlighted pane to choose its edge point.</p>
      <div id="cut-locate-result"></div>`,
  };
}

export function locateResultMarkup(query) {
  return `<div class="metrics">
    ${stat("Stock sheet", `#${query.sheet}`)}
    ${stat("From left edge", `${n(query.distance_from_left_mm)} mm`)}
    ${stat("From top edge", `${n(query.distance_from_top_mm)} mm`)}
    ${stat("From right edge", `${n(query.distance_from_right_mm)} mm`)}
    ${stat("From bottom edge", `${n(query.distance_from_bottom_mm)} mm`)}
    ${stat("Turned 180°", query.turned_180 ? "yes" : "no")}
  </div>
  <p class="soft">Grain angle ${n(query.grain_angle_deg, 2)}° · plane position ${n(query.plane_mm[0])}, ${n(query.plane_mm[1])} mm. This lookup is recorded on the frozen plan.</p>`;
}
