'use strict';
// ════════════════════════════════════════════════════════
// Video Autopilot — Dashboard JS
// Vanilla, no build step, no external deps.
// ════════════════════════════════════════════════════════

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));

// ── api helper ───────────────────────────────────────────
async function api(path, opts = {}) {
  const r = await fetch('/api' + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const error = new Error(data.detail || `HTTP ${r.status}`);
    error.status = r.status;
    throw error;
  }
  return data;
}

// ── niche colours ────────────────────────────────────────
const NICHE_COLOR = {
  motivation:'#f472b6', finance:'#4ade80', money:'#4ade80',
  psychology:'#60a5fa', history:'#fb923c', ai:'#a78bfa',
  tech:'#a78bfa', health:'#2dd4bf', mystery:'#f87171',
  stoic:'#94a3b8', business:'#fbbf24', facts:'#60a5fa',
  default:'#7c5cf6',
};
const nicheColor = (niche='') =>
  NICHE_COLOR[niche.toLowerCase()] || NICHE_COLOR.default;

// ── toast ────────────────────────────────────────────────
function toast(msg, type = 'info') {
  const icons = { success:'✓', error:'✗', info:'→' };
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span class="toast-icon">${icons[type]??'•'}</span>
    <span class="toast-body">${esc(msg)}</span>
    <button class="toast-close">✕</button>`;
  $('#toastStack').prepend(t);
  const close = () => { t.classList.add('out'); setTimeout(() => t.remove(), 260); };
  t.querySelector('.toast-close').onclick = close;
  setTimeout(close, 4500);
}

// ── navigation ───────────────────────────────────────────
const VIEWS = {};
$$('.nav-item').forEach(item => {
  item.addEventListener('click', () => navigateTo(item.dataset.view));
});

function navigateTo(name) {
  $$('.nav-item').forEach(i => i.classList.toggle('active', i.dataset.view === name));
  $$('.view').forEach(v => v.classList.toggle('active', v.id === `view-${name}`));
  VIEWS[name]?.();
}
window.navigateTo = navigateTo;

// ════════════════════════════════════════════════════════
// VIDEO AUTOPILOT STUDIO
// ════════════════════════════════════════════════════════
const STUDIO_PROJECT_KEY = 'video-autopilot-studio-project';
let studioProjects = [];
let studioProject = null;
let studioMedia = [];
let studioScenes = [];
let studioSuggestions = {};
let studioAutoReview = false;
let studioAutoReviewSceneIds = [];
let studioAutoReviewSubmitting = false;
let studioExports = [];
let studioRenderJob = null;
let studioOutputProfiles = {};
let studioRenderTimer = null;
let studioAutoWorkflow = false;
let studioAutoStoryWorkflow = false;
let studioAutoStorySaveTimer = null;

function studioSceneHasValidMedia(scene) {
  const shots = Array.isArray(scene.shots) ? scene.shots : [];
  return shots.length
    ? shots.every(shot => studioMedia.some(item => item.id === shot.media_id))
    : Boolean(scene.media_id && studioMedia.some(item => item.id === scene.media_id));
}

function studioShotReviewSignature(scene) {
  const shots = Array.isArray(scene.shots) ? scene.shots : [];
  return JSON.stringify([
    Number(scene.duration).toFixed(6),
    ...shots.map(shot => [
    shot.media_id,
    Number(shot.start).toFixed(6),
    Number(shot.duration).toFixed(6),
    ]),
  ]);
}

function studioSceneIsReviewed(scene) {
  return scene.approved === true &&
    studioSceneHasValidMedia(scene) &&
    scene.reviewed_media_id === scene.media_id &&
    (!scene.shots?.length ||
      scene.reviewed_shots === studioShotReviewSignature(scene));
}

function mediaUrl(item) {
  return `/api/studio/media/${studioProject.id}/${item.id}`;
}

function showStudioPreview(url, kind, title) {
  const stage = $('#studioPreviewStage');
  if (kind === 'image') {
    stage.innerHTML = `<img src="${esc(url)}" alt="${esc(title)}"/>`;
  } else {
    stage.innerHTML = `<video src="${esc(url)}" controls playsinline preload="metadata"></video>`;
  }
  $('#previewCaption').textContent = title;
}

function renderStudioMedia() {
  const grid = $('#studioMediaGrid');
  $('#mediaCount').textContent = studioMedia.length;
  $('#masterStoryBrief').disabled = !studioProject;
  $('#useUploadOrderFallback').disabled = !studioProject;
  $('#replaceReviewedAssignments').disabled = !studioProject;
  renderStudioScenes();
  updateStudioRenderButton();
  if (!studioProject) {
    grid.innerHTML = '<div class="studio-empty">Create or open a project to start adding media.</div>';
    return;
  }
  if (!studioMedia.length) {
    grid.innerHTML = '<div class="studio-empty">No media in this project yet. Upload JPG, PNG, WEBP, or MP4 files above.</div>';
    return;
  }
  grid.innerHTML = studioMedia.map((item, index) => {
    const url = mediaUrl(item);
    const isVideo = item.extension === '.mp4';
    const preview = item.thumbnail_url
      ? `<img src="${esc(item.thumbnail_url)}" alt="Representative frame from ${esc(item.name)}" loading="lazy"/>`
      : (isVideo
        ? `<video src="${esc(url)}" muted preload="metadata"></video>`
        : `<img src="${esc(url)}" alt="${esc(item.name)}" loading="lazy"/>`);
    const technical = item.technical_metadata || {};
    const technicalSummary = technical.extraction_error
      ? `Technical metadata unavailable: ${technical.extraction_error}`
      : [
        technical.duration_seconds != null &&
          Number.isFinite(Number(technical.duration_seconds))
          ? `${Number(technical.duration_seconds).toFixed(1)} sec`
          : null,
        technical.width && technical.height ? `${technical.width}×${technical.height}` : null,
        technical.frame_rate ? `${technical.frame_rate} FPS` : null,
      ].filter(Boolean).join(' · ') || 'Technical metadata unavailable';
    return `<article class="studio-media-item" data-media-index="${index}">
      <button class="studio-media-preview" data-media-preview="${index}" aria-label="Preview ${esc(item.name)}">${preview}<span class="media-type">${isVideo ? 'VIDEO' : 'IMAGE'}</span></button>
      <div class="studio-media-meta"><span class="studio-media-name" title="${esc(item.name)}">${esc(item.name)}</span><span class="studio-media-size">${(item.size / (1024 * 1024)).toFixed(1)} MB</span></div>
      <div class="studio-technical-meta"><strong>Technical metadata (FFprobe):</strong> ${esc(technicalSummary)}<br/>Clip meaning comes from saved descriptions; thumbnails are previews only.${technical.thumbnail_error ? `<br/>Thumbnail unavailable: ${esc(technical.thumbnail_error)}` : ''}</div>
      <div class="studio-media-metadata" data-media-metadata="${index}">
        <label>Description<textarea class="studio-input" data-media-meta="description" rows="2" maxlength="2000">${esc(item.description || '')}</textarea></label>
        <label>Keywords<input class="studio-input" data-media-meta="keywords" maxlength="1000" value="${esc((item.keywords || []).join(', '))}" placeholder="comma-separated"/></label>
        <label>Characters<input class="studio-input" data-media-meta="characters" maxlength="700" value="${esc((item.characters || []).join(', '))}" placeholder="comma-separated"/></label>
        <label>Location<input class="studio-input" data-media-meta="location" maxlength="300" value="${esc(item.location || '')}"/></label>
        <label>Mood<input class="studio-input" data-media-meta="mood" maxlength="300" value="${esc(item.mood || '')}"/></label>
      </div>
      <div class="studio-media-controls">
        <button class="media-order-btn" data-media-move="-1" data-media-index="${index}" aria-label="Move up" ${index === 0 ? 'disabled' : ''}>↑</button>
        <button class="media-order-btn" data-media-move="1" data-media-index="${index}" aria-label="Move down" ${index === studioMedia.length - 1 ? 'disabled' : ''}>↓</button>
        <button class="media-remove-btn" data-media-remove="${index}">Remove</button>
      </div>
    </article>`;
  }).join('');

  $$('[data-media-preview]').forEach(button => button.addEventListener('click', () => {
    const item = studioMedia[Number(button.dataset.mediaPreview)];
    showStudioPreview(mediaUrl(item), item.extension === '.mp4' ? 'video' : 'image', item.name);
  }));
  $$('[data-media-move]').forEach(button => button.addEventListener('click', () => {
    moveStudioMedia(Number(button.dataset.mediaIndex), Number(button.dataset.mediaMove));
  }));
  $$('[data-media-remove]').forEach(button => button.addEventListener('click', () => {
    removeStudioMedia(studioMedia[Number(button.dataset.mediaRemove)]);
  }));
  $$('[data-media-metadata]').forEach(form => {
    form.querySelectorAll('[data-media-meta]').forEach(input => {
      input.addEventListener('change', () => saveStudioMediaMetadata(
        studioMedia[Number(form.dataset.mediaMetadata)], form,
      ));
    });
  });
}

function metadataList(value) {
  return value.split(/[,\n]/).map(item => item.trim()).filter(Boolean);
}

async function saveStudioMediaMetadata(item, form, notify = true) {
  if (!item || !studioProject) return;
  const value = name => form.querySelector(`[data-media-meta="${name}"]`).value;
  const metadata = {
    description: value('description'),
    keywords: metadataList(value('keywords')),
    characters: metadataList(value('characters')),
    location: value('location'),
    mood: value('mood'),
  };
  try {
    const updated = await api(`/studio/projects/${studioProject.id}/media/${item.id}/metadata`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(metadata),
    });
    Object.assign(item, updated);
    studioSuggestions = {};
    studioAutoReview = false;
    studioAutoReviewSceneIds = [];
    studioScenes.forEach(scene => {
      if (scene.assignment_source === 'auto') scene.approved = false;
    });
    renderStudioScenes();
    if (notify) toast(`Metadata saved for ${item.name}`, 'success');
    return true;
  } catch (e) {
    toast(`Could not save clip metadata: ${e.message}`, 'error');
    return false;
  }
}

async function saveVisibleStudioMetadata() {
  for (const form of $$('[data-media-metadata]')) {
    const item = studioMedia[Number(form.dataset.mediaMetadata)];
    if (!item) continue;
    const payload = {
      description: form.querySelector('[data-media-meta="description"]').value.trim(),
      keywords: metadataList(form.querySelector('[data-media-meta="keywords"]').value),
      characters: metadataList(form.querySelector('[data-media-meta="characters"]').value),
      location: form.querySelector('[data-media-meta="location"]').value.trim(),
      mood: form.querySelector('[data-media-meta="mood"]').value.trim(),
    };
    const saved = {
      description: item.description || '',
      keywords: metadataList(Array.isArray(item.keywords) ? item.keywords.join(', ') : item.keywords || ''),
      characters: metadataList(Array.isArray(item.characters) ? item.characters.join(', ') : item.characters || ''),
      location: item.location || '',
      mood: item.mood || '',
    };
    if (JSON.stringify(payload) !== JSON.stringify(saved) &&
        !await saveStudioMediaMetadata(item, form, false)) {
      throw new Error(`Could not save metadata for ${item.name}`);
    }
  }
}

function studioAudioSettings() {
  return {
    voiceover_enabled: $('#studioVoiceoverEnabled').checked,
    captions_enabled: $('#studioCaptionsEnabled').checked,
    voice: $('#studioVoice').value,
    speech_buffer: Number($('#studioSpeechBuffer').value),
    caption_size: $('#studioCaptionSize').value,
    caption_position: $('#studioCaptionPosition').value,
  };
}

function updateStudioAudioControls() {
  const enabled = $('#studioVoiceoverEnabled').checked;
  $('#studioVoice').disabled = !enabled;
  $('#studioSpeechBuffer').disabled = !enabled;
  const captionsEnabled = $('#studioCaptionsEnabled').checked;
  $('#studioCaptionSize').disabled = !captionsEnabled;
  $('#studioCaptionPosition').disabled = !captionsEnabled;
}

function studioAutoStoryInput() {
  return {
    master_story_brief: $('#masterStoryBrief').value,
    use_upload_order_fallback: $('#useUploadOrderFallback').checked,
    adaptive_pacing: $('#adaptivePacing').checked,
    audio_settings: studioAudioSettings(),
  };
}

function renderAutoStoryPlan(plan = [], required = [], orderingNote = '') {
  const container = $('#autoStoryPlan');
  const messages = required.map(message => `<li>${esc(message)}</li>`).join('');
  const rows = plan.map(scene => `<tr>
    <td>${esc(scene.scene_number)}</td>
    <td>${esc(scene.text)}</td>
    <td>${esc(scene.asset_name || 'Needs input')}</td>
    <td><strong>${esc(scene.basis)}</strong><small>${esc(scene.details || '')}</small></td>
  </tr>`).join('');
  container.innerHTML = `${orderingNote ? `<p class="studio-story-note">${esc(orderingNote)}</p>` : ''}
    ${messages ? `<div class="studio-auto-input-required"><strong>Needs input</strong><ul>${messages}</ul></div>` : ''}
    ${rows ? `<div class="studio-kicker">SCENE-TO-ASSET PLAN · ACTUAL MATCH BASIS</div><div class="studio-auto-plan-table-wrap"><table class="studio-auto-plan-table"><thead><tr><th>Scene</th><th>Narration</th><th>Selected upload</th><th>Basis</th></tr></thead><tbody>${rows}</tbody></table></div>` : ''}`;
}

function renderSavedAutoStoryPlan() {
  const plan = studioScenes.filter(scene => scene.match_basis).map(scene => ({
    scene_number: scene.scene_number || studioScenes.indexOf(scene) + 1,
    text: scene.text,
    asset_name: studioMedia.find(item => item.id === scene.media_id)?.name,
    basis: scene.match_basis,
    details: scene.match_details,
  }));
  renderAutoStoryPlan(plan);
}

function setStudioMode(mode) {
  const auto = mode === 'auto';
  $('#studioAutoModePanel').hidden = !auto;
  $('#studioManualModePanel').hidden = auto;
  $('#autoModeTab').classList.toggle('active', auto);
  $('#manualModeTab').classList.toggle('active', !auto);
  $('#autoModeTab').setAttribute('aria-selected', String(auto));
  $('#manualModeTab').setAttribute('aria-selected', String(!auto));
}

function scheduleStudioAutoStorySave() {
  if (!studioProject) return;
  if (studioAutoStorySaveTimer) clearTimeout(studioAutoStorySaveTimer);
  const projectId = studioProject.id;
  studioAutoStorySaveTimer = setTimeout(async () => {
    try {
      await api(`/studio/projects/${projectId}/auto-story/input`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(studioAutoStoryInput()),
      });
      if (studioProject?.id === projectId) {
        $('#autoStoryInputStatus').textContent = 'Master brief and audio settings saved to this project.';
      }
    } catch (e) {
      if (studioProject?.id === projectId) {
        $('#autoStoryInputStatus').textContent = `Could not save Auto Mode inputs: ${e.message}`;
      }
    }
  }, 500);
}

function updateStudioStoryEstimate() {
  const total = studioScenes.length
    ? studioScenes.reduce((sum, scene) => sum + (Number(scene.duration) || 0), 0)
    : ($('#storyScript').value.trim()
      ? 0
      : studioMedia.length * Number($('#renderSceneSeconds').value || 0));
  $('#storyDurationEstimate').textContent =
    `Requested scene timeline: ${total.toFixed(1)} seconds; narration may extend it after TTS.`;
}

function updateStudioRenderButton() {
  const pending = studioScenes.some(scene =>
    !studioSceneIsReviewed(scene));
  const rendering = ['queued', 'running'].includes(studioRenderJob?.status);
  $('#studioGenerateBtn').disabled = !studioProject || !studioMedia.length ||
    pending || rendering;
  $('#autoCreateVideoBtn').disabled = !studioProject || !studioMedia.length ||
    rendering || $('#autoCreateVideoBtn').dataset.running === 'true';
  $('#createStoryVideoBtn').disabled = !studioProject || rendering ||
    $('#createStoryVideoBtn').dataset.running === 'true';
  $('#autoArrangeBtn').disabled = !studioProject || !studioMedia.length ||
    !studioScenes.length || rendering;
  const confirmButton = $('#confirmAutoReviewBtn');
  confirmButton.hidden = !studioAutoReview || !studioAutoReviewSceneIds.length;
  confirmButton.disabled = rendering || studioAutoReviewSubmitting ||
    !studioAutoReviewSceneIds.length ||
    !studioScenes.every(studioSceneIsReviewed) ||
    !studioAutoReviewSceneIds.every(id => {
      const scene = studioScenes.find(item => item.id === id);
      return scene && studioSceneIsReviewed(scene);
    });
}

function invalidateStudioSuggestions() {
  studioSuggestions = {};
  studioAutoReview = false;
  studioAutoReviewSceneIds = [];
  studioScenes.forEach(scene => {
    if (scene.assignment_source === 'auto') scene.approved = false;
  });
  $('#autoDirectorStatus').textContent =
    'Scene details changed. Re-run Auto Create Video or Auto Arrange to refresh rankings.';
  renderStudioScenes();
}

function renderStudioScenes() {
  const clipList = $('#studioRenderClipList');
  const enabled = Boolean(studioProject);
  $('#storyScript').disabled = !enabled;
  $('#buildStoryScenesBtn').disabled = !enabled;
  $('#saveStoryBtn').disabled = !enabled;
  $('#autoArrangeBtn').disabled = !enabled || !studioScenes.length;
  if (!studioScenes.length) {
    clipList.innerHTML = `<div class="studio-empty">${$('#storyScript').value.trim()
      ? 'Build scene blocks from your script before assigning media.'
      : 'Build scene blocks from a script, or leave the script empty for media-only rendering.'}</div>`;
    updateStudioStoryEstimate();
    updateStudioRenderButton();
    return;
  }
  clipList.innerHTML = studioScenes.map((scene, index) => {
    const selected = studioMedia.find(item => item.id === scene.media_id);
    const clipName = selected ? selected.name : 'No clip assigned';
    const assignedMediaOptions = studioMedia.map(item =>
      `<option value="${esc(item.id)}" ${item.id === scene.media_id ? 'selected' : ''}>${esc(item.name)} · ${item.extension === '.mp4' ? 'VIDEO' : 'IMAGE'}</option>`
    ).join('');
    const ranked = studioSuggestions[scene.id] || [];
    const needsReview = studioAutoReviewSceneIds.includes(scene.id);
    const rankedOptions = needsReview
      ? ranked.filter(suggestion => suggestion.matchable).slice(0, 5)
      : ranked.slice(0, 5);
    const suggestions = rankedOptions.map((suggestion, rank) => {
      const confidence = suggestion.confidence === 'high'
        ? (suggestion.auto_assignable
          ? 'Clear high overlap'
          : needsReview && rank === 0
            ? `High overlap · ${Number(suggestion.margin).toFixed(1)}pt lead over next`
            : 'High heuristic overlap')
        : suggestion.confidence === 'moderate'
          ? 'Moderate heuristic overlap' : 'Uncertain / Needs Review';
      const selectedForReview = scene.media_id === suggestion.media_id;
      return `<button class="studio-suggestion${selectedForReview ? ' selected' : ''}" type="button" data-suggest-scene="${esc(scene.id)}" data-suggest-media="${esc(suggestion.media_id)}" ${needsReview ? 'data-review-suggest="true"' : ''}>
        <span class="studio-suggestion-rank">${rank + 1}</span>
        <span class="studio-suggestion-body"><strong>${esc(suggestion.name)}</strong>
          <span>${Number(suggestion.score).toFixed(1)}% · ${confidence} · ${suggestion.metadata_fields}/5 saved metadata fields</span>
          <small>${esc(suggestion.reasons.join('; '))}</small>
        </span>
      </button>`;
    }).join('');
    const suggestionList = ranked.length
      ? `<div class="studio-suggestions"><div class="studio-kicker">${needsReview ? 'GROUNDED RANKED OPTIONS · SELECT FOR CONFIRMATION' : 'RANKED TEXT-METADATA SUGGESTIONS · SELECT TO ASSIGN'}</div>${suggestions}${!rankedOptions.length ? '<small>No grounded options are available. Add clip metadata or assign media manually.</small>' : ''}${!needsReview && ranked.length > 5 ? `<small>Showing top 5 of ${ranked.length} clips.</small>` : ''}</div>`
      : '';
    const shots = Array.isArray(scene.shots) ? scene.shots : [];
    let shotOffset = 0;
    const shotRows = shots.map((shot, shotIndex) => {
      const start = shotOffset;
      shotOffset += Number(shot.duration) || 0;
      const shotOptions = studioMedia.map(item =>
        `<option value="${esc(item.id)}" ${item.id === shot.media_id ? 'selected' : ''}>${esc(item.name)} · ${item.extension === '.mp4' ? 'VIDEO' : 'IMAGE'}</option>`
      ).join('');
      return `<div class="studio-shot-row">
        <label class="studio-scene-field">Shot ${shotIndex + 1} · ${start.toFixed(2)}–${shotOffset.toFixed(2)} sec
          <select class="select-sm" data-shot-media="${index}:${shotIndex}">
            <option value="">Select uploaded media</option>${shotOptions}
          </select>
        </label>
        <label class="studio-scene-field">Shot duration
          <span><input class="studio-input" data-shot-duration="${index}:${shotIndex}" type="number" min="0.1" max="600" step="0.1" value="${esc(shot.duration)}"/> sec</span>
        </label>
        <button class="btn-ghost btn-sm" type="button" data-shot-remove="${index}:${shotIndex}">Remove shot</button>
      </div>`;
    }).join('');
    return `<article class="studio-scene" data-scene-index="${index}">
      <div class="studio-scene-heading">
        <span class="studio-render-clip-index">${index + 1}</span>
        <strong>Scene ${index + 1}</strong>
        ${needsReview ? '<span class="studio-review-badge">Needs review</span>' : ''}
        <label class="studio-scene-approval"><input type="checkbox" data-scene-approved="${index}" ${studioSceneIsReviewed(scene) ? 'checked' : ''}/> I reviewed this assignment</label>
        <div class="studio-scene-order">
          <button class="media-order-btn" data-scene-move="-1" data-scene-index="${index}" aria-label="Move scene ${index + 1} up" ${index === 0 ? 'disabled' : ''}>↑</button>
          <button class="media-order-btn" data-scene-move="1" data-scene-index="${index}" aria-label="Move scene ${index + 1} down" ${index === studioScenes.length - 1 ? 'disabled' : ''}>↓</button>
        </div>
      </div>
      <label class="studio-scene-field">Scene text
        <textarea class="studio-input studio-scene-text" data-scene-text="${index}" rows="2" maxlength="5000">${esc(scene.text)}</textarea>
      </label>
      <div class="studio-scene-controls">
        <label class="studio-scene-field">Assigned media
          <select class="select-sm" data-scene-media="${index}">
            <option value="">Select uploaded media</option>${assignedMediaOptions}
          </select>
        </label>
        <label class="studio-scene-field studio-scene-duration">Duration
          <span><input class="studio-input" data-scene-duration="${index}" type="number" min="0.5" max="600" step="0.1" value="${esc(scene.duration)}"/> sec</span>
        </label>
      </div>
      <div class="studio-shot-editor">
        <div class="studio-shot-editor-heading"><strong>Scene shots</strong><button class="btn-ghost btn-sm" type="button" data-shot-add="${index}">Add shot</button></div>
        ${shots.length ? shotRows : '<small>Single-media scene. Add shots to explicitly assign multiple clips or graphics and set their durations.</small>'}
        ${shots.length ? `<small>Ordered coverage: ${shotOffset.toFixed(2)} sec of ${Number(scene.duration).toFixed(2)} sec. Images hold for their shot duration; video is never looped.</small>` : ''}
      </div>
      <details class="studio-scene-metadata">
        <summary>Optional scene matching metadata</summary>
        <div class="studio-scene-metadata-grid">
          <label class="studio-scene-field">Visual description
            <textarea class="studio-input" data-scene-metadata="${index}" data-scene-field="visual_description" rows="2" maxlength="2000">${esc(scene.visual_description || '')}</textarea>
          </label>
          <label class="studio-scene-field">Characters
            <input class="studio-input" data-scene-metadata="${index}" data-scene-field="characters" maxlength="700" value="${esc((scene.characters || []).join(', '))}" placeholder="comma-separated"/>
          </label>
          <label class="studio-scene-field">Location
            <input class="studio-input" data-scene-metadata="${index}" data-scene-field="location" maxlength="300" value="${esc(scene.location || '')}"/>
          </label>
          <label class="studio-scene-field">Mood
            <input class="studio-input" data-scene-metadata="${index}" data-scene-field="mood" maxlength="300" value="${esc(scene.mood || '')}"/>
          </label>
          <label class="studio-scene-field">Intended action
            <textarea class="studio-input" data-scene-metadata="${index}" data-scene-field="intended_action" rows="2" maxlength="1000">${esc(scene.intended_action || '')}</textarea>
          </label>
        </div>
      </details>
      ${suggestionList}
      <div class="studio-scene-timeline">
        <span class="studio-kicker">TIMELINE · ${String(index + 1).padStart(2, '0')}</span>
        <p class="studio-timeline-text">${esc(scene.text)}</p>
        <div><span class="studio-timeline-clip">${esc(clipName)} · ${scene.assignment_source === 'auto' ? 'Auto suggestion' : 'Manual assignment'}</span><span class="studio-timeline-duration">${Number(scene.duration).toFixed(1)} sec</span></div>
      </div>
    </article>`;
  }).join('');

  $$('[data-scene-text]').forEach(input => input.addEventListener('input', () => {
    const index = Number(input.dataset.sceneText);
    studioScenes[index].text = input.value;
    const summary = input.closest('.studio-scene').querySelector('.studio-timeline-text');
    summary.textContent = input.value || 'Scene text';
  }));
  $$('[data-scene-text]').forEach(input =>
    input.addEventListener('change', invalidateStudioSuggestions));
  $$('[data-scene-metadata]').forEach(input => {
    const update = () => {
      const scene = studioScenes[Number(input.dataset.sceneMetadata)];
      const field = input.dataset.sceneField;
      scene[field] = field === 'characters' ? metadataList(input.value) : input.value;
    };
    input.addEventListener('input', update);
    input.addEventListener('change', invalidateStudioSuggestions);
  });
  $$('[data-scene-media]').forEach(select => select.addEventListener('change', () => {
    const index = Number(select.dataset.sceneMedia);
    studioScenes[index].media_id = select.value || null;
    delete studioScenes[index].shots;
    studioScenes[index].assignment_source = 'manual';
    studioScenes[index].match_basis = null;
    studioScenes[index].match_details = '';
    studioScenes[index].approved = false;
    studioScenes[index].reviewed_media_id = null;
    studioScenes[index].reviewed_shots = null;
    updateStudioRenderButton();
    renderStudioScenes();
  }));
  $$('[data-scene-duration]').forEach(input => input.addEventListener('input', () => {
    const index = Number(input.dataset.sceneDuration);
    const duration = Number(input.value);
    if (Number.isFinite(duration) && duration >= 0.5 && duration <= 600) {
      studioScenes[index].duration = duration;
      studioScenes[index].approved = false;
      studioScenes[index].reviewed_media_id = null;
      studioScenes[index].reviewed_shots = null;
      input.closest('.studio-scene').querySelector('.studio-timeline-duration').textContent =
        `${duration.toFixed(1)} sec`;
      const approval = input.closest('.studio-scene').querySelector('[data-scene-approved]');
      if (approval) approval.checked = false;
      updateStudioStoryEstimate();
      updateStudioRenderButton();
    }
  }));
  $$('[data-shot-add]').forEach(button => button.addEventListener('click', () => {
    const index = Number(button.dataset.shotAdd);
    const scene = studioScenes[index];
    if (!Array.isArray(scene.shots)) {
      scene.shots = scene.media_id
        ? [{ media_id: scene.media_id, start: 0, duration: Number(scene.duration) }]
        : [];
    }
    const previous = scene.shots[scene.shots.length - 1];
    const transfer = previous
      ? Math.min(1, Math.max((Number(previous.duration) || 0) - 0.1, 0))
      : 0;
    if (previous && transfer > 0) previous.duration -= transfer;
    const nextStart = scene.shots.reduce(
      (sum, shot) => sum + (Number(shot.duration) || 0), 0,
    );
    scene.shots.push({
      media_id: '',
      start: nextStart,
      duration: transfer || Math.min(1, Number(scene.duration)),
    });
    scene.approved = false;
    scene.reviewed_media_id = null;
    scene.reviewed_shots = null;
    renderStudioScenes();
  }));
  $$('[data-shot-remove]').forEach(button => button.addEventListener('click', () => {
    const [sceneIndex, shotIndex] = button.dataset.shotRemove.split(':').map(Number);
    const scene = studioScenes[sceneIndex];
    scene.shots.splice(shotIndex, 1);
    scene.shots.forEach((shot, index) => {
      shot.start = scene.shots.slice(0, index).reduce(
        (sum, previous) => sum + (Number(previous.duration) || 0), 0,
      );
    });
    if (!scene.shots.length) delete scene.shots;
    scene.media_id = scene.shots?.[0]?.media_id || null;
    scene.approved = false;
    scene.reviewed_media_id = null;
    scene.reviewed_shots = null;
    renderStudioScenes();
  }));
  $$('[data-shot-media]').forEach(select => select.addEventListener('change', () => {
    const [sceneIndex, shotIndex] = select.dataset.shotMedia.split(':').map(Number);
    const scene = studioScenes[sceneIndex];
    scene.shots[shotIndex].media_id = select.value;
    scene.media_id = scene.shots[0]?.media_id || null;
    scene.assignment_source = 'manual';
    scene.approved = false;
    scene.reviewed_media_id = null;
    scene.reviewed_shots = null;
    renderStudioScenes();
  }));
  $$('[data-shot-duration]').forEach(input => input.addEventListener('change', () => {
    const [sceneIndex, shotIndex] = input.dataset.shotDuration.split(':').map(Number);
    const duration = Number(input.value);
    if (!Number.isFinite(duration) || duration <= 0 || duration > 600) return;
    const scene = studioScenes[sceneIndex];
    scene.shots[shotIndex].duration = duration;
    scene.shots.forEach((shot, index) => {
      shot.start = scene.shots.slice(0, index).reduce(
        (sum, previous) => sum + (Number(previous.duration) || 0), 0,
      );
    });
    scene.approved = false;
    scene.reviewed_media_id = null;
    scene.reviewed_shots = null;
    renderStudioScenes();
  }));
  $$('[data-scene-move]').forEach(button => button.addEventListener('click', () => {
    moveStudioScene(Number(button.dataset.sceneIndex), Number(button.dataset.sceneMove));
  }));
  $$('[data-scene-approved]').forEach(input => input.addEventListener('change', () => {
    const scene = studioScenes[Number(input.dataset.sceneApproved)];
    scene.approved = Boolean(input.checked && studioSceneHasValidMedia(scene));
    scene.reviewed_media_id = scene.approved ? scene.media_id : null;
    scene.reviewed_shots = scene.approved && scene.shots?.length
      ? studioShotReviewSignature(scene) : null;
    input.checked = studioSceneIsReviewed(scene);
    updateStudioRenderButton();
  }));
  $$('[data-suggest-media]').forEach(button => button.addEventListener('click', () => {
    const scene = studioScenes.find(item => item.id === button.dataset.suggestScene);
    if (!scene) return;
    if (button.dataset.reviewSuggest) {
      scene.media_id = button.dataset.suggestMedia;
      delete scene.shots;
      scene.assignment_source = 'manual';
      scene.match_basis = null;
      scene.match_details = '';
      scene.approved = false;
      scene.reviewed_media_id = null;
      scene.reviewed_shots = null;
      renderStudioScenes();
      return;
    }
    scene.media_id = button.dataset.suggestMedia;
    delete scene.shots;
    scene.assignment_source = 'manual';
    scene.match_basis = null;
    scene.match_details = '';
    scene.approved = false;
    scene.reviewed_media_id = null;
    scene.reviewed_shots = null;
    renderStudioScenes();
  }));
  updateStudioStoryEstimate();
  updateStudioRenderButton();
}

function buildStudioScenes() {
  const lines = $('#storyScript').value.split(/\r?\n+/).map(line => line.trim()).filter(Boolean);
  studioScenes = lines.map((text, index) => {
    const existing = studioScenes[index];
    return {
      id: existing?.id || crypto.randomUUID().replaceAll('-', ''),
      text,
      media_id: existing?.media_id || null,
      duration: existing?.duration || Number($('#renderSceneSeconds').value) || 2.3,
      shots: existing?.shots || [],
      visual_description: existing?.visual_description || '',
      characters: existing?.characters || [],
      location: existing?.location || '',
      mood: existing?.mood || '',
      intended_action: existing?.intended_action || '',
      assignment_source: existing?.assignment_source || 'manual',
      approved: existing?.approved ?? Boolean(existing?.media_id),
      reviewed_media_id: existing?.reviewed_media_id ??
        ((existing?.approved ?? Boolean(existing?.media_id))
          ? existing.media_id : null),
    };
  });
  studioSuggestions = {};
  $('#autoDirectorStatus').textContent = '';
  renderStudioScenes();
}

async function saveStudioStory() {
  if (!studioProject) return;
  const saveButton = $('#saveStoryBtn');
  saveButton.disabled = true;
  try {
    const result = await api(`/studio/projects/${studioProject.id}/story`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        script: $('#storyScript').value,
        scenes: studioScenes,
        audio_settings: studioAudioSettings(),
        master_story_brief: $('#masterStoryBrief').value,
      }),
    });
    studioScenes = result.scenes;
    renderStudioScenes();
    return true;
  } catch (e) {
    toast('Could not save story: ' + e.message, 'error');
    return false;
  } finally {
    saveButton.disabled = !studioProject;
  }
}

async function autoArrangeStudioScenes() {
  if (!studioProject || !studioScenes.length) return;
  const hasAssignments = studioScenes.some(scene => scene.media_id);
  const confirmOverwrite = hasAssignments && window.confirm(
    'Auto Arrange will replace existing scene assignments. Continue?',
  );
  if (hasAssignments && !confirmOverwrite) return;
  const button = $('#autoArrangeBtn');
  button.disabled = true;
  $('#autoDirectorStatus').textContent = 'Ranking clips against saved scene text and metadata…';
  try {
    if (!await saveStudioStory()) {
      $('#autoDirectorStatus').textContent = 'Could not save scene changes; review the reported save error.';
      return;
    }
    const result = await api(`/studio/projects/${studioProject.id}/auto-director/arrange`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm_overwrite: confirmOverwrite }),
    });
    studioScenes = result.scenes;
    studioSuggestions = Object.fromEntries(
      result.suggestions.map(item => [item.scene_id, item.ranked_clips]),
    );
    const uncertain = studioScenes.filter(scene => !scene.media_id).length;
    $('#autoDirectorStatus').textContent = uncertain
      ? `Suggestions ready. ${uncertain} scene${uncertain === 1 ? '' : 's'} are Uncertain / Needs Review and remain unassigned.`
      : 'Suggestions ready. Every suggested assignment needs your review and approval.';
    renderStudioScenes();
    toast('Deterministic clip suggestions are ready for review', 'success');
  } catch (e) {
    $('#autoDirectorStatus').textContent = `Auto Arrange failed: ${e.message}`;
    toast('Auto Arrange failed: ' + e.message, 'error');
  } finally {
    updateStudioRenderButton();
  }
}

function studioRenderSettings() {
  return {
    aspect_ratio: $('#renderAspectRatio').value,
    fps: Number($('#renderFps').value),
    seconds_per_scene: Number($('#renderSceneSeconds').value),
    export_preset: $('#exportPreset').value,
    image_motion: $('#imageMotion').value === 'true',
    transition: $('#sceneTransition').value,
  };
}

function updateStudioOutputProfile() {
  const profile = studioOutputProfiles[$('#renderAspectRatio').value];
  if (!profile) throw new Error('Unsupported output format');
  $('#renderResolution').value =
    `${profile.width} × ${profile.height} · ${profile.label}`;
}

async function saveStudioQualitySettings() {
  if (!studioProject) return;
  const projectId = studioProject.id;
  const status = $('#qualitySettingsStatus');
  status.textContent = 'Saving quality options…';
  try {
    await api(`/studio/projects/${projectId}/quality-settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        aspect_ratio: $('#renderAspectRatio').value,
        image_motion: $('#imageMotion').value === 'true',
        transition: $('#sceneTransition').value,
        export_preset: $('#exportPreset').value,
      }),
    });
    if (studioProject?.id === projectId) {
      status.textContent = 'Output format and quality options saved to this project.';
    }
  } catch (e) {
    if (studioProject?.id === projectId) {
      status.textContent = `Could not save quality options: ${e.message}`;
      toast('Could not save quality options: ' + e.message, 'error');
    }
  }
}

function setStudioWorkflowStatus(status, message) {
  const statusEl = $('#studioJobStatus');
  statusEl.dataset.status = status;
  statusEl.textContent = `${status.replace('_', ' ')}: ${message}`;
  const directorStatus = $('#autoDirectorStatus');
  directorStatus.dataset.status = status;
  directorStatus.textContent = `${status.replace('_', ' ')}: ${message}`;
}

async function monitorStudioRender(job) {
  studioRenderJob = job;
  await loadStudioJobStatus();
  if (studioRenderJob &&
      ['queued', 'running'].includes(studioRenderJob.status)) {
    if (studioRenderTimer) clearInterval(studioRenderTimer);
    studioRenderTimer = setInterval(loadStudioJobStatus, 1500);
  }
}

async function autoCreateStudioVideo() {
  if (!studioProject || !studioMedia.length ||
      ['queued', 'running'].includes(studioRenderJob?.status)) return;
  const button = $('#autoCreateVideoBtn');
  button.dataset.running = 'true';
  studioAutoWorkflow = true;
  button.disabled = true;
  studioAutoReview = false;
  studioAutoReviewSceneIds = [];
  studioAutoReviewSubmitting = false;
  studioAutoStoryWorkflow = false;
  try {
    if (!studioScenes.length && $('#storyScript').value.trim()) {
      buildStudioScenes();
    }
    setStudioWorkflowStatus('analyzing', 'saving current clip descriptions and project settings');
    await saveVisibleStudioMetadata();
    if (!await saveStudioStory()) {
      throw new Error('Could not save story edits');
    }
    setStudioWorkflowStatus('matching', 'comparing saved clip metadata with story scenes');
    const result = await api(`/studio/projects/${studioProject.id}/auto-director/create`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(studioRenderSettings()),
    });
    studioScenes = (result.scenes || []).map(scene => ({
      ...scene,
      characters: Array.isArray(scene.characters) ? scene.characters : [],
      reviewed_media_id: scene.reviewed_media_id ??
        (scene.approved === true ? scene.media_id : null),
    }));
    studioSuggestions = Object.fromEntries(
      (result.suggestions || []).map(item => [item.scene_id, item.ranked_clips]),
    );
    studioAutoReviewSceneIds = result.review_scene_ids || [];
    studioAutoReview = result.status === 'needs_review';
    renderStudioScenes();
    if (studioAutoReview) {
      const count = studioAutoReviewSceneIds.length;
      setStudioWorkflowStatus(
        'needs_review',
        `${count} scene${count === 1 ? '' : 's'} need confirmation. Assign an uploaded clip to each scene and review every assignment, then confirm once to render.`,
      );
      updateStudioRenderButton();
      return;
    }
    await monitorStudioRender(result.job);
  } catch (e) {
    setStudioWorkflowStatus('failed', e.message);
    toast('Auto Create Video failed: ' + e.message, 'error');
  } finally {
    button.dataset.running = 'false';
    updateStudioRenderButton();
  }
}

async function createStudioStoryVideo() {
  if (!studioProject || ['queued', 'running'].includes(studioRenderJob?.status)) return;
  const previousJobId = studioRenderJob?.id;
  const brief = $('#masterStoryBrief').value.trim();
  if (!brief) {
    $('#autoStoryInputStatus').textContent = 'Needs input: enter a Master Story Brief.';
    $('#masterStoryBrief').focus();
    return;
  }
  const button = $('#createStoryVideoBtn');
  if (studioAutoStorySaveTimer) {
    clearTimeout(studioAutoStorySaveTimer);
    studioAutoStorySaveTimer = null;
  }
  button.dataset.running = 'true';
  studioAutoWorkflow = false;
  studioAutoReview = false;
  studioAutoReviewSceneIds = [];
  studioAutoReviewSubmitting = false;
  studioAutoStoryWorkflow = true;
  button.disabled = true;
  $('#autoStoryInputStatus').textContent = 'Planning: saving your brief and project settings…';
  try {
    await api(`/studio/projects/${studioProject.id}/auto-story/input`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(studioAutoStoryInput()),
    });
    $('#autoStoryInputStatus').textContent = 'Planning: resolving numbered scenes and asset references…';
    const request = {
      ...studioAutoStoryInput(),
      replace_reviewed_assignments: $('#replaceReviewedAssignments').checked,
      render_settings: studioRenderSettings(),
    };
    const preview = await api(`/studio/projects/${studioProject.id}/auto-story/create`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...request, preview_only: true }),
    });
    renderAutoStoryPlan(
      preview.plan || [], preview.input_required || [], preview.ordering_note || '',
    );
    if (preview.scenes) {
      studioScenes = preview.scenes;
      renderStudioScenes();
    }
    if (preview.status === 'needs_input') {
      $('#autoStoryInputStatus').textContent = 'Needs input: no render was started.';
      studioAutoStoryWorkflow = false;
      return;
    }
    if (preview.status !== 'preview') {
      throw new Error('The server did not prepare a scene preview.');
    }

    $('#autoStoryInputStatus').textContent = 'Planning: preview ready; starting the local render…';
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const result = await api(`/studio/projects/${studioProject.id}/auto-story/create`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
    if (result.plan) {
      renderAutoStoryPlan(
        result.plan, result.input_required || [], result.ordering_note || '',
      );
    }
    if (result.scenes) {
      studioScenes = result.scenes;
      renderStudioScenes();
    }
    if (result.status === 'needs_input') {
      $('#autoStoryInputStatus').textContent = 'Needs input: no render was started.';
      studioAutoStoryWorkflow = false;
      return;
    }
    if (result.status !== 'rendering' || !result.job) {
      throw new Error('The server did not start a render job.');
    }
    $('#autoStoryInputStatus').textContent = 'Preparing: the scene plan is ready; the local render job has started.';
    await monitorStudioRender(result.job);
  } catch (e) {
    if (e.status === 409 || e.message.includes('already queued or running')) {
      studioAutoStoryWorkflow = false;
      $('#autoStoryInputStatus').textContent = `Rendering: ${e.message}`;
      await loadStudioJobStatus();
      if (['queued', 'running'].includes(studioRenderJob?.status)) {
        await monitorStudioRender(studioRenderJob);
      }
    } else if (e.status >= 400 && e.status < 500) {
      studioAutoStoryWorkflow = false;
      $('#autoStoryInputStatus').textContent = `Needs input: ${e.message}`;
    } else {
      studioAutoStoryWorkflow = false;
      await loadStudioJobStatus();
      const latest = studioRenderJob;
      if (latest?.id && latest.id !== previousJobId &&
          ['queued', 'running'].includes(latest.status)) {
        studioAutoStoryWorkflow = true;
        await monitorStudioRender(latest);
      } else if (latest?.id && latest.id !== previousJobId &&
                 latest.status === 'failed') {
        $('#autoStoryInputStatus').textContent = `Failed: ${latest.error || e.message}`;
      } else if (latest?.id && latest.id !== previousJobId &&
                 latest.status === 'completed') {
        $('#autoStoryInputStatus').textContent = 'Completed';
      } else {
        $('#autoStoryInputStatus').textContent =
          `Could not start or verify render: ${e.message}`;
      }
      toast('Create Story Video failed: ' + e.message, 'error');
    }
  } finally {
    button.dataset.running = 'false';
    updateStudioRenderButton();
  }
}

async function confirmStudioAutoReview() {
  if (!studioProject || !studioAutoReviewSceneIds.length ||
      studioAutoReviewSubmitting ||
      !studioScenes.every(studioSceneIsReviewed) ||
      !studioAutoReviewSceneIds.every(id => {
        const scene = studioScenes.find(item => item.id === id);
        return scene && studioSceneIsReviewed(scene);
      })) return;
  const button = $('#confirmAutoReviewBtn');
  studioAutoReviewSubmitting = true;
  button.disabled = true;
  setStudioWorkflowStatus('matching', 'validating reviewed scene assignments');
  try {
    if (!await saveStudioStory()) {
      throw new Error('Could not save story edits');
    }
    const result = await api(`/studio/projects/${studioProject.id}/auto-director/resume`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        assignments: Object.fromEntries(
          studioScenes.map(scene => [scene.id, scene.media_id]),
        ),
        render_settings: studioRenderSettings(),
      }),
    });
    studioScenes = result.scenes;
    studioAutoReview = false;
    studioAutoReviewSceneIds = [];
    renderStudioScenes();
    await monitorStudioRender(result.job);
  } catch (e) {
    setStudioWorkflowStatus('needs_review', e.message);
    toast('Could not continue Auto Create: ' + e.message, 'error');
    updateStudioRenderButton();
  } finally {
    studioAutoReviewSubmitting = false;
    updateStudioRenderButton();
  }
}

function moveStudioScene(index, offset) {
  const target = index + offset;
  if (target < 0 || target >= studioScenes.length) return;
  [studioScenes[index], studioScenes[target]] = [studioScenes[target], studioScenes[index]];
  renderStudioScenes();
}

async function loadStudioProject(projectId) {
  if (studioAutoStorySaveTimer) {
    clearTimeout(studioAutoStorySaveTimer);
    studioAutoStorySaveTimer = null;
    if (studioProject) {
      try {
        await api(`/studio/projects/${studioProject.id}/auto-story/input`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(studioAutoStoryInput()),
        });
      } catch (e) {
        $('#autoStoryInputStatus').textContent = `Could not save Auto Mode inputs: ${e.message}`;
        toast('Could not save Auto Mode inputs before switching projects: ' + e.message, 'error');
      }
    }
  }
  if (!projectId) {
    studioProject = null;
    studioOutputProfiles = {};
    studioMedia = [];
    studioScenes = [];
    studioSuggestions = {};
    studioAutoReview = false;
    studioAutoReviewSceneIds = [];
    studioAutoReviewSubmitting = false;
    studioRenderJob = null;
    studioAutoWorkflow = false;
    studioAutoStoryWorkflow = false;
    $('#masterStoryBrief').value = '';
    $('#useUploadOrderFallback').checked = false;
    $('#replaceReviewedAssignments').checked = false;
    $('#autoStoryInputStatus').textContent = '';
    renderAutoStoryPlan();
    if (studioRenderTimer) clearInterval(studioRenderTimer);
    $('#studioJobStatus').textContent = 'Select a project to see its render status.';
    $('#studioJobStatus').dataset.status = 'idle';
    $('#storyScript').value = '';
    $('#studioVoiceoverEnabled').checked = false;
    $('#studioCaptionsEnabled').checked = false;
    updateStudioAudioControls();
    renderStudioMedia();
    renderSavedAutoStoryPlan();
    return;
  }
  try {
    const project = await api(`/studio/projects/${projectId}`);
    studioProject = project;
    studioOutputProfiles = project.output_profiles || {};
    studioMedia = project.media.map(item => ({
      description: '',
      keywords: [],
      characters: [],
      location: '',
      mood: '',
      technical_metadata: {},
      ...item,
    }));
    $('#storyScript').value = project.story_script || '';
    studioScenes = (project.scenes || []).map(scene => {
      const approved = scene.approved ?? Boolean(scene.media_id);
      return {
        visual_description: '',
        characters: [],
        location: '',
        mood: '',
        intended_action: '',
        assignment_source: 'manual',
        ...scene,
        characters: Array.isArray(scene.characters) ? scene.characters : [],
        approved,
        reviewed_media_id: scene.reviewed_media_id ??
          (approved && !(scene.assignment_source === 'auto' && scene.match_basis)
            ? scene.media_id : null),
      };
    });
    studioSuggestions = {};
    studioAutoReview = false;
    studioAutoReviewSceneIds = [];
    studioAutoReviewSubmitting = false;
    $('#autoDirectorStatus').textContent = '';
    $('#masterStoryBrief').value = project.master_story_brief || '';
    const autoSettings = project.auto_mode_settings || {};
    $('#useUploadOrderFallback').checked =
      Boolean(autoSettings.use_upload_order_fallback);
    $('#replaceReviewedAssignments').checked = false;
    $('#adaptivePacing').checked = Boolean(autoSettings.adaptive_pacing);
    $('#autoStoryInputStatus').textContent = '';
    const audioSettings = project.audio_settings || {};
    $('#studioVoiceoverEnabled').checked = Boolean(audioSettings.voiceover_enabled);
    $('#studioCaptionsEnabled').checked = Boolean(audioSettings.captions_enabled);
    $('#studioVoice').value = audioSettings.voice || 'en-US-AriaNeural';
    $('#studioSpeechBuffer').value = audioSettings.speech_buffer ?? 0.5;
    $('#studioCaptionSize').value = audioSettings.caption_size || 'medium';
    $('#studioCaptionPosition').value = audioSettings.caption_position || 'lower';
    const qualitySettings = project.quality_settings || {};
    $('#imageMotion').value = String(qualitySettings.image_motion ?? true);
    $('#sceneTransition').value = qualitySettings.transition || 'hard_cut';
    $('#exportPreset').value = qualitySettings.export_preset ||
      project.render_defaults.export_preset || 'legacy';
    $('#renderAspectRatio').value = qualitySettings.aspect_ratio ||
      project.render_defaults.aspect_ratio || project.aspect_ratio || '9:16';
    updateStudioOutputProfile();
    $('#qualitySettingsStatus').textContent =
      'Output format and quality options saved to this project.';
    updateStudioAudioControls();
    studioRenderJob = null;
    studioAutoWorkflow = false;
    studioAutoStoryWorkflow = false;
    const fpsSelect = $('#renderFps');
    const configuredFps = String(project.render_defaults.fps);
    if (![...fpsSelect.options].some(option => option.value === configuredFps)) {
      fpsSelect.add(new Option(`${configuredFps} FPS · configured`, configuredFps));
    }
    fpsSelect.value = configuredFps;
    $('#renderSceneSeconds').value = project.render_defaults.seconds_per_scene;
    localStorage.setItem(STUDIO_PROJECT_KEY, project.id);
    $('#uploadNotice').textContent = '';
    renderStudioMedia();
    renderSavedAutoStoryPlan();
    $('#uploadDropzone').classList.remove('disabled');
    $('#mediaFiles').disabled = false;
    await loadStudioJobStatus(project.id);
  } catch (e) {
    toast('Could not open project: ' + e.message, 'error');
  }
}

async function loadStudioProjects(preferredId = localStorage.getItem(STUDIO_PROJECT_KEY)) {
  studioProjects = await api('/studio/projects');
  const select = $('#projectSelect');
  select.innerHTML = studioProjects.length
    ? studioProjects.map(project => `<option value="${esc(project.id)}">${esc(project.name)}</option>`).join('')
    : '<option value="">No projects yet</option>';
  const selected = studioProjects.some(project => project.id === preferredId)
    ? preferredId : studioProjects[0]?.id || '';
  select.value = selected;
  await loadStudioProject(selected);
  $('#mediaFiles').disabled = !selected;
  $('#uploadDropzone').classList.toggle('disabled', !selected);
}

async function createStudioProject() {
  const input = $('#projectName');
  const name = input.value.trim();
  if (!name) {
    toast('Enter a project name first', 'error');
    input.focus();
    return;
  }
  $('#createProjectBtn').disabled = true;
  try {
    const project = await api('/studio/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    input.value = '';
    await loadStudioProjects(project.id);
    toast('Project created', 'success');
  } catch (e) {
    toast('Could not create project: ' + e.message, 'error');
  } finally {
    $('#createProjectBtn').disabled = false;
  }
}

const STUDIO_MAX_FILE_SIZE = 200 * 1024 * 1024;
const STUDIO_EXTENSIONS = new Set(['.jpg', '.jpeg', '.png', '.webp', '.mp4']);

async function uploadStudioFiles(fileList) {
  if (!studioProject) {
    toast('Create or open a project before uploading', 'error');
    return;
  }
  for (const file of fileList) {
    const ext = `.${file.name.split('.').pop().toLowerCase()}`;
    if (!STUDIO_EXTENSIONS.has(ext)) {
      toast(`${file.name}: unsupported file type`, 'error');
      continue;
    }
    if (file.size > STUDIO_MAX_FILE_SIZE) {
      toast(`${file.name}: exceeds the 200 MB limit`, 'error');
      continue;
    }
    const notice = $('#uploadNotice');
    notice.textContent = `Uploading ${file.name}…`;
    const form = new FormData();
    form.append('file', file, file.name);
    try {
      await api(`/studio/projects/${studioProject.id}/media`, { method: 'POST', body: form });
      await loadStudioProject(studioProject.id);
      notice.textContent = `${file.name} uploaded`;
    } catch (e) {
      notice.textContent = `${file.name}: ${e.message}`;
      toast(`Upload failed: ${e.message}`, 'error');
    }
  }
  $('#mediaFiles').value = '';
}

async function moveStudioMedia(index, offset) {
  const target = index + offset;
  if (target < 0 || target >= studioMedia.length) return;
  const reordered = [...studioMedia];
  [reordered[index], reordered[target]] = [reordered[target], reordered[index]];
  try {
    await api(`/studio/projects/${studioProject.id}/media/order`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ media_ids: reordered.map(item => item.id) }),
    });
    studioMedia = reordered;
    renderStudioMedia();
  } catch (e) { toast('Could not reorder media: ' + e.message, 'error'); }
}

async function removeStudioMedia(item) {
  if (!item || !confirm(`Remove ${item.name} from this project?`)) return;
  try {
    await api(`/studio/projects/${studioProject.id}/media/${item.id}`, { method: 'DELETE' });
    await loadStudioProject(studioProject.id);
    toast('Media removed', 'info');
  } catch (e) { toast('Could not remove media: ' + e.message, 'error'); }
}

function exportVideoUrl(rel, endpoint = 'video') {
  return `/api/${endpoint}/${rel.split('/').map(encodeURIComponent).join('/')}`;
}

async function loadStudioExports(preferredExportId = null) {
  try {
    const selectedExport = $('#exportSelect').value;
    const previousUrl = selectedExport === ''
      ? null : studioExports[Number(selectedExport)]?.video_url;
    studioExports = await api('/studio/exports');
    const select = $('#exportSelect');
    select.innerHTML = studioExports.length
      ? `<option value="">Select a completed video</option>` + studioExports.map((item, index) =>
        `<option value="${index}">${esc(item.title)} · ${esc(item.date)}</option>`).join('')
      : '<option value="">No completed MP4 videos yet</option>';
    select.disabled = !studioExports.length;
    const preferredIndex = studioExports.findIndex(item =>
      (preferredExportId && item.id === preferredExportId) ||
      (!preferredExportId && previousUrl && item.video_url === previousUrl));
    if (preferredIndex >= 0) select.value = String(preferredIndex);
    updateStudioExportActions();
  } catch (e) {
    $('#exportSelect').innerHTML = '<option value="">Could not load completed videos</option>';
    $('#exportInfo').textContent = e.message;
  }
}

function updateStudioExportActions() {
  const selected = $('#exportSelect').value;
  const item = selected === '' ? null : studioExports[Number(selected)];
  const previewButton = $('#previewExportBtn');
  const downloadButton = $('#downloadExportBtn');
  previewButton.disabled = !item;
  downloadButton.classList.toggle('disabled', !item);
  downloadButton.setAttribute('aria-disabled', item ? 'false' : 'true');
  if (item) {
    $('#exportInfo').textContent = `${item.title} · ${item.channel} · ${item.date}`;
    downloadButton.href = item.download_url || exportVideoUrl(item.rel, 'download');
    downloadButton.download = 'video.mp4';
  } else {
    $('#exportInfo').textContent = studioExports.length
      ? 'Choose a finished video to preview or download.'
      : 'Completed MP4 files from the existing output library appear here.';
    downloadButton.removeAttribute('href');
  }
}

async function loadStudioJobStatus() {
  const statusEl = $('#studioJobStatus');
  if (!studioProject) {
    statusEl.textContent = 'Select a project to see its render status.';
    statusEl.dataset.status = 'idle';
    return;
  }
  const projectId = studioProject.id;
  try {
    const latest = await api(`/studio/projects/${projectId}/render`);
    if (studioProject?.id !== projectId) return;
    studioRenderJob = latest.status === 'idle' ? null : latest;
    const status = latest.status || 'idle';
    const displayStatus = ['queued', 'running'].includes(status)
      ? 'rendering' : status;
    statusEl.dataset.status = displayStatus;
    if (studioAutoWorkflow && status !== 'idle') {
      const directorStatus = $('#autoDirectorStatus');
      directorStatus.dataset.status = displayStatus;
      directorStatus.textContent = `Auto Create Video ${displayStatus}${latest.error ? `: ${latest.error}` : ''}`;
    }
    if (studioAutoStoryWorkflow && status !== 'idle') {
      const stage = {
        preparing: 'Preparing',
        synthesizing_voiceover: 'Synthesizing voiceover',
        rendering: 'Rendering',
        completed: 'Completed',
        failed: 'Failed',
      }[latest.stage] || displayStatus;
      const details = latest.error ? `: ${latest.error}` :
        latest.duration ? ` · ${Number(latest.duration).toFixed(1)} seconds` : '';
      $('#autoStoryInputStatus').textContent = `${stage}${details}`;
    }
    if (status === 'idle') {
      statusEl.textContent = 'No render has been started for this project.';
    } else {
        const extended = (latest.scene_timings || []).filter(timing => timing.extended)
          .map(timing => `Scene ${timing.scene} ${Number(timing.requested_duration).toFixed(1)}s → ${Number(timing.output_duration).toFixed(1)}s for ${Number(timing.narration_duration).toFixed(1)}s speech`);
        const timeline = latest.duration != null
          ? ` · Actual ${Number(latest.duration).toFixed(1)}s`
          : latest.estimated_duration != null
            ? ` · Estimated ${Number(latest.estimated_duration).toFixed(1)}s`
            : '';
        const fileSize = latest.size_bytes
          ? ` · ${(Number(latest.size_bytes) / (1024 * 1024)).toFixed(1)} MiB`
          : '';
        statusEl.textContent = `Render ${displayStatus}${latest.finished ? ` · finished ${latest.finished}` : ''}${timeline}${extended.length ? ` · ${extended.join('; ')}` : ''}${latest.error ? ` · ${latest.error}` : ''}`;
        if (latest.duration != null && fileSize) statusEl.textContent += fileSize;
    }
    updateStudioRenderButton();
    if (status === 'completed') loadStudioExports(latest.export_id);
    if (['completed', 'failed', 'idle'].includes(status) && studioRenderTimer) {
      clearInterval(studioRenderTimer);
      studioRenderTimer = null;
    }
  } catch (e) {
    statusEl.textContent = `Render status unavailable: ${e.message}`;
    statusEl.dataset.status = 'failed';
  }
}

async function loadStudio() {
  try {
    await Promise.all([loadStudioProjects(), loadStudioExports()]);
  } catch (e) {
    toast('Studio could not load: ' + e.message, 'error');
  }
}

$('#createProjectBtn').addEventListener('click', createStudioProject);
$('#projectName').addEventListener('keydown', event => {
  if (event.key === 'Enter') createStudioProject();
});
$('#projectSelect').addEventListener('change', event => loadStudioProject(event.target.value));
$('#mediaFiles').addEventListener('change', event => uploadStudioFiles(event.target.files));
const uploadDropzone = $('#uploadDropzone');
['dragenter', 'dragover'].forEach(eventName => uploadDropzone.addEventListener(eventName, event => {
  event.preventDefault();
  if (studioProject) uploadDropzone.classList.add('drag-over');
}));
['dragleave', 'drop'].forEach(eventName => uploadDropzone.addEventListener(eventName, event => {
  event.preventDefault();
  uploadDropzone.classList.remove('drag-over');
}));
uploadDropzone.addEventListener('drop', event => uploadStudioFiles(event.dataTransfer.files));
$('#exportSelect').addEventListener('change', updateStudioExportActions);
$('#buildStoryScenesBtn').addEventListener('click', buildStudioScenes);
$('#saveStoryBtn').addEventListener('click', saveStudioStory);
$('#autoArrangeBtn').addEventListener('click', autoArrangeStudioScenes);
$('#autoCreateVideoBtn').addEventListener('click', autoCreateStudioVideo);
$('#createStoryVideoBtn').addEventListener('click', createStudioStoryVideo);
$('#confirmAutoReviewBtn').addEventListener('click', confirmStudioAutoReview);
$$('[data-studio-mode]').forEach(button => button.addEventListener('click', () =>
  setStudioMode(button.dataset.studioMode)));
$('#masterStoryBrief').addEventListener('input', () => {
  $('#autoStoryInputStatus').textContent = 'Saving Master Story Brief…';
  renderAutoStoryPlan();
  scheduleStudioAutoStorySave();
});
$('#useUploadOrderFallback').addEventListener('change', scheduleStudioAutoStorySave);
$('#adaptivePacing').addEventListener('change', scheduleStudioAutoStorySave);
$('#storyScript').addEventListener('input', updateStudioStoryEstimate);
$('#renderSceneSeconds').addEventListener('input', updateStudioStoryEstimate);
$('#renderAspectRatio').addEventListener('change', () => {
  updateStudioOutputProfile();
  saveStudioQualitySettings();
});
['imageMotion', 'sceneTransition', 'exportPreset'].forEach(id =>
  $(`#${id}`).addEventListener('change', saveStudioQualitySettings));
$('#studioVoiceoverEnabled').addEventListener('change', () => {
  updateStudioAudioControls();
  scheduleStudioAutoStorySave();
});
$('#studioCaptionsEnabled').addEventListener('change', () => {
  updateStudioAudioControls();
  scheduleStudioAutoStorySave();
});
['studioVoice', 'studioSpeechBuffer', 'studioCaptionSize', 'studioCaptionPosition']
  .forEach(id => $(`#${id}`).addEventListener('change', scheduleStudioAutoStorySave));
$('#previewExportBtn').addEventListener('click', () => {
  const item = studioExports[Number($('#exportSelect').value)];
  if (item) showStudioPreview(item.video_url || exportVideoUrl(item.rel), 'video', item.title);
});
$('#studioGenerateBtn').addEventListener('click', async () => {
  if (!studioProject) return;
  studioAutoWorkflow = false;
  const seconds = Number($('#renderSceneSeconds').value);
  if (!Number.isFinite(seconds) || seconds < 0.5 || seconds > 30) {
    toast('Seconds per scene must be between 0.5 and 30', 'error');
    return;
  }
  $('#studioGenerateBtn').disabled = true;
  try {
    if (!await saveStudioStory()) {
      $('#studioGenerateBtn').disabled = false;
      return;
    }
    const job = await api(`/studio/projects/${studioProject.id}/render`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(studioRenderSettings()),
    });
    await monitorStudioRender(job);
    toast('Local render queued', 'success');
  } catch (e) {
    $('#studioGenerateBtn').disabled = false;
    toast('Could not start render: ' + e.message, 'error');
  }
});
$$('[data-open-view]').forEach(button => button.addEventListener('click', () => navigateTo(button.dataset.openView)));

// ════════════════════════════════════════════════════════
// DASHBOARD
// ════════════════════════════════════════════════════════
let channelData = [];

async function loadDashboard() {
  const [analytics, envSt] = await Promise.all([
    api('/analytics').catch(() => ({ total_videos:0, channels:{}, recent_runs:[] })),
    api('/env-status').catch(() => ({})),
  ]);

  renderStats(analytics);
  renderSetupBanner(envSt, analytics);
  renderActivity(analytics.recent_runs);
  renderQuickChannels();
  loadSidebarKeys(envSt);
}

function animateNum(el, to) {
  const dur = 700, start = performance.now();
  const tick = now => {
    const p = Math.min(1, (now - start) / dur);
    el.textContent = Math.round(p * to);
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function renderStats(a) {
  const items = [
    { icon:'🎬', label:'Videos produced', value: a.total_videos,      cls:'purple' },
    { icon:'📺', label:'Active channels',  value: Object.keys(a.channels).length, cls:'cyan' },
    { icon:'🚀', label:'Published',        value: a.recent_runs.filter(r=>r.status==='posted').length, cls:'green' },
    { icon:'⚡', label:'Dry-runs built',   value: a.recent_runs.filter(r=>r.status==='dry_run').length, cls:'orange' },
  ];
  $('#statsRow').innerHTML = items.map(it =>
    `<div class="stat-card">
      <div class="stat-icon ${it.cls}">${it.icon}</div>
      <div class="stat-num" data-val="${it.value}">0</div>
      <div class="stat-lbl">${esc(it.label)}</div>
    </div>`).join('');
  $$('.stat-num').forEach(el => animateNum(el, +el.dataset.val));
}

function renderSetupBanner(env, analytics) {
  const hasLLM     = env.ANTHROPIC_API_KEY || env.OPENAI_API_KEY;
  const hasVisuals = env.PEXELS_API_KEY || env.FAL_KEY || env.REPLICATE_API_TOKEN;
  const hasVideos  = analytics.total_videos > 0;
  const hasPublish = env.AYRSHARE_API_KEY || env.UPLOAD_POST_API_KEY;

  const steps = [
    { title: 'Add an LLM key', desc: 'Claude (Anthropic) or OpenAI — powers idea generation & scripting. Paste into your .env file.', done: !!hasLLM, action: 'settings' },
    { title: 'Add a Pexels key (free)', desc: 'Free stock B-roll for your videos. Sign up at pexels.com/api — takes 60 seconds.', done: !!hasVisuals, action: 'settings' },
    { title: 'Generate your first video', desc: 'Go to Generate → pick a channel → keep Dry-run ON → hit Start. A video appears in Library.', done: hasVideos, action: 'generate' },
    { title: 'Connect a publish provider', desc: 'Ayrshare posts to YouTube, TikTok, Instagram & Facebook with a single API token.', done: !!hasPublish, action: 'settings' },
  ];

  const done = steps.filter(s => s.done).length;
  const banner = $('#setupBanner');
  if (done === steps.length) { banner.style.display = 'none'; return; }
  banner.style.display = '';
  $('#setupProgress').textContent = `${done} of ${steps.length} complete`;
  $('#setupSteps').innerHTML = steps.map(s =>
    `<div class="setup-step ${s.done?'done':''}" data-action="${s.action}">
      <div class="step-check">${s.done?'✓':''}</div>
      <div class="step-info">
        <div class="step-title">${esc(s.title)}</div>
        <div class="step-desc">${esc(s.desc)}</div>
      </div>
    </div>`).join('');
  $$('.setup-step').forEach(el => {
    el.addEventListener('click', () => navigateTo(el.dataset.action));
  });
}

function renderActivity(runs) {
  const feed = $('#activityFeed');
  if (!runs?.length) {
    feed.innerHTML = `<div class="empty-state">
      <div class="empty-state-icon">📭</div>
      <h4>No activity yet</h4>
      <p>Generate your first video to see it here.</p>
    </div>`;
    return;
  }
  feed.innerHTML = runs.slice(0,14).map(r =>
    `<div class="activity-item">
      <div class="activity-dot ${r.status}"></div>
      <div class="activity-body">
        <div class="activity-title">${esc(r.title || r.slug || r.channel)}</div>
        <div class="activity-meta">${esc(r.channel)} · ${esc(r.status)}</div>
      </div>
      <span class="status-pill ${r.status}">${esc(r.status)}</span>
    </div>`).join('');
}

function renderQuickChannels() {
  const qc = $('#quickChannels');
  if (!channelData.length) { qc.innerHTML = '<div class="empty-state"><p>No channels configured</p></div>'; return; }
  qc.innerHTML = channelData.map(c =>
    `<div class="quick-ch" data-key="${esc(c.key)}">
      <div class="quick-ch-dot" style="background:${nicheColor(c.niche)}"></div>
      <div class="quick-ch-info">
        <div class="quick-ch-name">${esc(c.name)}</div>
        <div class="quick-ch-sub">${esc(c.niche)} · ${c.produced} videos</div>
      </div>
      <button class="quick-ch-btn">▶ Run</button>
    </div>`).join('');
  $$('.quick-ch').forEach(el => {
    el.addEventListener('click', () => quickGenerate(el.dataset.key));
  });
}

function loadSidebarKeys(env) {
  const labels = {
    ANTHROPIC_API_KEY:'Claude', OPENAI_API_KEY:'OpenAI',
    PEXELS_API_KEY:'Pexels', ELEVENLABS_API_KEY:'ElevenLabs',
    AYRSHARE_API_KEY:'Ayrshare',
  };
  $('#sidebarKeys').innerHTML =
    `<div class="sidebar-keys-title">API Keys</div>` +
    Object.entries(labels).map(([k,l]) =>
      `<div class="key-row">
        <span>${l}</span>
        <span class="key-dot ${env[k]?'on':'off'}"></span>
      </div>`).join('');
}

$('#refreshDash').addEventListener('click', loadDashboard);
$('#quickGenBtn').addEventListener('click', () => navigateTo('generate'));

// ════════════════════════════════════════════════════════
// GENERATE
// ════════════════════════════════════════════════════════
let selectedChannel = null;
let genCount = 1;
let activeJobId = null;
let pollTimer = null;

const STAGES = ['ideation','script','voiceover','visuals','captions','assemble','metadata','publish'];
const STAGE_PATTERNS = {
  ideation:  /ideation|idea:/i,
  script:    /script:|scriptwriter/i,
  voiceover: /voiceover:|voice\.mp3/i,
  visuals:   /visuals:|clips downloaded/i,
  captions:  /captions:|captions\.ass/i,
  assemble:  /assemble:|final\.mp4/i,
  metadata:  /metadata:|title:/i,
  publish:   /publish|posting|posted|dry.run/i,
};

function renderGenChannelPicker() {
  const picker = $('#genChannelPicker');
  picker.innerHTML = channelData.map(c =>
    `<div class="ch-pick ${c.key===selectedChannel?'selected':''}" data-key="${esc(c.key)}">
      <div class="ch-pick-dot" style="background:${nicheColor(c.niche)}"></div>
      <span class="ch-pick-name">${esc(c.name)}</span>
      <span class="ch-pick-niche">${esc(c.niche)}</span>
    </div>`).join('');
  $$('.ch-pick').forEach(el => {
    el.addEventListener('click', () => {
      selectedChannel = el.dataset.key;
      $$('.ch-pick').forEach(p => p.classList.toggle('selected', p.dataset.key===selectedChannel));
      if ($('#genAll').checked) $('#genAll').click(); // uncheck all
    });
  });
  if (!selectedChannel && channelData.length) selectedChannel = channelData[0].key;
  $$('.ch-pick').forEach(p => p.classList.toggle('selected', p.dataset.key===selectedChannel));
}

// count buttons
$('#countDown').addEventListener('click', () => {
  if (genCount > 1) { genCount--; $('#countVal').textContent = genCount; }
});
$('#countUp').addEventListener('click', () => {
  if (genCount < 10) { genCount++; $('#countVal').textContent = genCount; }
});

$('#genAll').addEventListener('change', e => {
  const on = e.target.checked;
  $('#countDown').disabled = on;
  $('#countUp').disabled = on;
  $$('.ch-pick').forEach(el => el.style.opacity = on ? '0.4' : '1');
});

$('#clearLog').addEventListener('click', () => {
  $('#logOutput').textContent = 'Log cleared.';
  resetStages();
});

function resetStages() {
  $$('.stage').forEach(s => s.className = 'stage');
}

function setStage(name, state) {
  const el = $(`.stage[data-stage="${name}"]`);
  if (el) el.className = `stage ${state}`;
}

function updateStagesFromLog(logs) {
  let lastDone = -1;
  STAGES.forEach((s, i) => {
    const matched = logs.some(l => STAGE_PATTERNS[s]?.test(l));
    if (matched) lastDone = i;
  });
  STAGES.forEach((s, i) => {
    if (i < lastDone) setStage(s, 'done');
    else if (i === lastDone) setStage(s, 'done');
    else if (i === lastDone + 1) setStage(s, 'active');
    else setStage(s, '');
  });
}

$('#genBtn').addEventListener('click', async () => {
  const allChannels = $('#genAll').checked;
  if (!allChannels && !selectedChannel) { toast('Pick a channel first', 'error'); return; }

  $('#genBtn').disabled = true;
  resetStages();
  setStage('ideation', 'active');
  $('#logOutput').textContent = 'Starting pipeline…\n';
  $('#logStatus').className = 'status-pill running';
  $('#logStatus').textContent = 'running';
  $('#logTitle').textContent = `Live log — ${allChannels ? 'all channels' : selectedChannel}`;

  try {
    const { job_id } = await api('/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        channel: selectedChannel,
        count: genCount,
        dry_run: $('#genDry').checked,
        all_channels: allChannels,
      }),
    });
    activeJobId = job_id;
    updateRunningBadge(true);
    pollJob(job_id);
  } catch(e) {
    toast('Failed to start: ' + e.message, 'error');
    $('#genBtn').disabled = false;
    setLogStatus('error');
  }
});

function setLogStatus(st) {
  $('#logStatus').className = `status-pill ${st}`;
  $('#logStatus').textContent = st;
}

function pollJob(jid) {
  clearInterval(pollTimer);
  const logEl = $('#logOutput');

  const tick = async () => {
    try {
      const j = await api(`/jobs/${jid}`);
      const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60;
      logEl.textContent = j.logs.join('\n');
      if (atBottom) logEl.scrollTop = logEl.scrollHeight;

      updateStagesFromLog(j.logs);

      if (j.status !== 'running') {
        clearInterval(pollTimer);
        activeJobId = null;
        updateRunningBadge(false);
        setLogStatus(j.status);
        $('#genBtn').disabled = false;

        if (j.status === 'done') {
          STAGES.forEach(s => setStage(s, 'done'));
          toast('Video generated successfully!', 'success');
          loadLibraryData(); // refresh library
        } else {
          toast('Pipeline encountered an error — check the log', 'error');
        }
      }
    } catch {
      clearInterval(pollTimer);
      $('#genBtn').disabled = false;
    }
  };

  tick();
  pollTimer = setInterval(tick, 1800);
}

function updateRunningBadge(on) {
  const b = $('#runningBadge');
  b.style.display = on ? '' : 'none';
  if (on) b.textContent = '•';
}

function quickGenerate(channelKey) {
  navigateTo('generate');
  selectedChannel = channelKey;
  renderGenChannelPicker();
  setTimeout(() => $('#genBtn').click(), 120);
}

// ════════════════════════════════════════════════════════
// LIBRARY
// ════════════════════════════════════════════════════════
let libraryItems = [];
let libSearch = '';
let libFilter = '';

async function loadLibraryData() {
  libraryItems = await api('/library').catch(() => []);
  renderLibrary();
  populateLibFilter();
}

function populateLibFilter() {
  const channels = [...new Set(libraryItems.map(i => i.channel).filter(Boolean))];
  const sel = $('#libFilter');
  const cur = sel.value;
  sel.innerHTML = `<option value="">All channels</option>` +
    channels.map(c => `<option value="${esc(c)}" ${c===cur?'selected':''}>${esc(c)}</option>`).join('');
}

function renderLibrary() {
  const grid = $('#libraryGrid');
  let items = libraryItems;
  if (libSearch) items = items.filter(i => i.title.toLowerCase().includes(libSearch.toLowerCase()));
  if (libFilter) items = items.filter(i => i.channel === libFilter);

  if (!items.length) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;padding:80px 24px">
      <div class="empty-state-icon">🎞️</div>
      <h4>${libSearch||libFilter ? 'No matches found' : 'No videos yet'}</h4>
      <p>${libSearch||libFilter ? 'Try a different search or filter.' : 'Generate your first video — hit the Generate tab above!'}</p>
    </div>`;
    return;
  }

  grid.innerHTML = items.map(it => {
    const color = nicheColor(it.niche || it.channel);
    return `<div class="video-card" data-rel="${esc(it.rel)}">
      <div class="video-thumb">
        ${it.has_video
          ? `<img src="/api/thumbnail/${it.rel}" loading="lazy" onerror="this.parentElement.innerHTML='<div class=\\'video-thumb-placeholder\\'><div class=\\'thumb-icon\\'>🎬</div><div>No thumbnail</div></div><div class=\\'thumb-play\\'><div class=\\'thumb-play-btn\\'><svg viewBox=\\'0 0 24 24\\' width=\\'22\\' height=\\'22\\' fill=\\'white\\'><polygon points=\\'5 3 19 12 5 21 5 3\\'/></svg></div></div>'">`
          : `<div class="video-thumb-placeholder"><div class="thumb-icon">⏳</div><div>Processing</div></div>`}
        ${it.has_video ? `<div class="thumb-play"><div class="thumb-play-btn"><svg viewBox="0 0 24 24" width="22" height="22" fill="white"><polygon points="5 3 19 12 5 21 5 3"/></svg></div></div>` : ''}
      </div>
      <div class="video-card-body">
        <div class="video-card-ch" style="color:${color}">${esc(it.channel)}</div>
        <div class="video-card-title">${esc(it.title)}</div>
        <div class="video-card-meta">
          <span class="video-card-date">${esc(it.date)}</span>
          <button class="video-card-del" data-rel="${esc(it.rel)}" title="Delete" onclick="event.stopPropagation()">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>
          </button>
        </div>
      </div>
    </div>`;
  }).join('');

  $$('.video-card').forEach(card => {
    card.addEventListener('click', () => openVideoModal(card.dataset.rel));
  });
  $$('.video-card-del').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm('Delete this video?')) return;
      try {
        await api(`/library/${btn.dataset.rel}`, { method: 'DELETE' });
        toast('Video deleted', 'info');
        await loadLibraryData();
      } catch(err) { toast('Delete failed: ' + err.message, 'error'); }
    });
  });
}

$('#libSearch').addEventListener('input', e => { libSearch = e.target.value; renderLibrary(); });
$('#libFilter').addEventListener('change', e => { libFilter = e.target.value; renderLibrary(); });

async function openVideoModal(rel) {
  const modal = $('#videoModal');
  $('#modalVideo').src = `/api/video/${rel}`;
  modal.classList.add('open');
  try {
    const m = await api(`/library/${rel}/meta`);
    const md = m.metadata || {};
    const tags = (md.hashtags || []).map(t => `<span class="modal-tag">${esc(t)}</span>`).join('');
    $('#modalInfo').innerHTML = `
      <h4>${esc(md.title || 'Untitled')}</h4>
      <p>${esc(md.description || '')}</p>
      ${tags ? `<div class="modal-tags">${tags}</div>` : ''}
      ${md.pinned_comment ? `<p class="text-muted text-sm mt-12"><b>Pinned comment:</b> ${esc(md.pinned_comment)}</p>` : ''}`;
  } catch { $('#modalInfo').innerHTML = ''; }
}

$('#modalClose').addEventListener('click', () => {
  $('#videoModal').classList.remove('open');
  $('#modalVideo').pause();
  $('#modalVideo').src = '';
});
$('#videoModal').addEventListener('click', e => {
  if (e.target === $('#videoModal')) $('#modalClose').click();
});

// ════════════════════════════════════════════════════════
// CHANNELS
// ════════════════════════════════════════════════════════
function renderChannels() {
  const grid = $('#channelCards');
  if (!channelData.length) {
    grid.innerHTML = `<div class="empty-state"><p>No channels configured. Edit channels.yaml in Settings.</p></div>`;
    return;
  }
  grid.innerHTML = channelData.map(c => {
    const color = nicheColor(c.niche);
    const topics = (c.recent_topics || []).reverse().slice(0,3);
    return `<div class="ch-card">
      <div class="ch-card-stripe" style="background:linear-gradient(90deg,${color},transparent)"></div>
      <div class="ch-card-body">
        <span class="ch-card-niche" style="color:${color};background:${color}18">${esc(c.niche)}</span>
        <div class="ch-card-name">${esc(c.name)}</div>
        <div class="ch-card-tone">${esc(c.tone || '')}</div>
        <div class="ch-card-stats">
          <div class="ch-stat"><div class="ch-stat-num">${c.produced}</div><div class="ch-stat-lbl">Produced</div></div>
          <div class="ch-stat"><div class="ch-stat-num">${c.posts_per_day}</div><div class="ch-stat-lbl">Per day</div></div>
          <div class="ch-stat"><div class="ch-stat-num">${(c.platforms||[]).length}</div><div class="ch-stat-lbl">Platforms</div></div>
        </div>
        <div class="ch-platforms">${(c.platforms||[]).map(p=>`<span class="plat-tag">${esc(p)}</span>`).join('')}</div>
        ${topics.length ? `<div class="ch-recent">
          <div class="ch-recent-title">Recent topics</div>
          ${topics.map(t=>`<div class="ch-topic">${esc(t)}</div>`).join('')}
        </div>` : ''}
        <div class="ch-actions">
          <button class="btn-primary btn-sm" data-key="${esc(c.key)}">▶ Generate</button>
          <button class="btn-ghost btn-sm" onclick="navigateTo('settings')">Edit</button>
        </div>
      </div>
    </div>`;
  }).join('');
  $$('.ch-card .btn-primary').forEach(btn => {
    btn.addEventListener('click', () => quickGenerate(btn.dataset.key));
  });
}

// ════════════════════════════════════════════════════════
// GUIDE
// ════════════════════════════════════════════════════════
let guideLoaded = false;
async function loadGuide() {
  if (guideLoaded) return;
  try {
    const html = await fetch('guide.html').then(r => r.text());
    $('#guideContent').innerHTML = html;
    guideLoaded = true;
    initGuideFaq();
    initGuideTocHighlight();
  } catch(e) {
    $('#guideContent').innerHTML = `<p style="color:var(--text2);padding:24px">Could not load guide: ${esc(e.message)}</p>`;
  }
}
function initGuideFaq() {
  $$('.faq-item .faq-q').forEach(q => {
    q.addEventListener('click', () => q.parentElement.classList.toggle('open'));
  });
}
function initGuideTocHighlight() {
  const links = $$('.toc-link');
  const observer = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) {
        links.forEach(l => l.classList.toggle('active', l.getAttribute('href') === `#${e.target.id}`));
      }
    });
  }, { rootMargin: '-20% 0px -60% 0px' });
  $$('.g-section[id]').forEach(s => observer.observe(s));
}

// ════════════════════════════════════════════════════════
// SETTINGS
// ════════════════════════════════════════════════════════
let activeCfg = 'settings.yaml';

$$('.stab').forEach(tab => {
  tab.addEventListener('click', () => {
    $$('.stab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    if (tab.dataset.cfg === 'keys') {
      $('#settingsEditor').classList.remove('active');
      $('#settingsEditor').style.display = 'none';
      $('#settingsKeys').style.display = '';
      loadKeyPanel();
    } else {
      $('#settingsEditor').style.display = '';
      $('#settingsKeys').style.display = 'none';
      loadCfgEditor(tab.dataset.cfg);
    }
  });
});

async function loadCfgEditor(name) {
  activeCfg = name;
  try {
    const { content } = await api(`/config/${name}`);
    $('#cfgEditor').value = content;
    $('#cfgMsg').textContent = '';
    $('#cfgMsg').className = 'save-msg';
  } catch(e) { toast('Load failed: ' + e.message, 'error'); }
}

$('#cfgSave').addEventListener('click', async () => {
  try {
    await api(`/config/${activeCfg}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: $('#cfgEditor').value }),
    });
    $('#cfgMsg').textContent = '✓ Saved successfully';
    $('#cfgMsg').className = 'save-msg ok';
    toast('Configuration saved', 'success');
    const ch = await api('/channels').catch(() => []);
    channelData = ch;
    renderQuickChannels();
    renderGenChannelPicker();
    renderChannels();
  } catch(e) {
    $('#cfgMsg').textContent = '✗ ' + e.message;
    $('#cfgMsg').className = 'save-msg err';
    toast('Save failed: ' + e.message, 'error');
  }
});

async function loadKeyPanel() {
  const env = await api('/env-status').catch(() => ({}));
  const KEYS = [
    { key:'ANTHROPIC_API_KEY', label:'Anthropic (Claude)', tag:'LLM' },
    { key:'OPENAI_API_KEY',    label:'OpenAI (GPT)',       tag:'LLM' },
    { key:'ELEVENLABS_API_KEY',label:'ElevenLabs',         tag:'Voice' },
    { key:'PEXELS_API_KEY',    label:'Pexels',             tag:'Visuals' },
    { key:'FAL_KEY',           label:'fal.ai',             tag:'AI Images' },
    { key:'REPLICATE_API_TOKEN',label:'Replicate',         tag:'AI Images' },
    { key:'AYRSHARE_API_KEY',  label:'Ayrshare',           tag:'Publishing' },
    { key:'UPLOAD_POST_API_KEY',label:'upload-post.com',   tag:'Publishing' },
  ];
  $('#keysGrid').innerHTML = KEYS.map(k =>
    `<div class="key-item">
      <div class="key-status-dot ${env[k.key]?'on':'off'}"></div>
      <div class="key-name">${esc(k.label)}</div>
      <span class="key-tag">${esc(k.tag)}</span>
      <span class="key-state ${env[k.key]?'on':'off'}">${env[k.key]?'Configured':'Missing'}</span>
    </div>`).join('');
  loadSidebarKeys(env);
}

// ════════════════════════════════════════════════════════
// VIEW REGISTRY + BOOT
// ════════════════════════════════════════════════════════
VIEWS.dashboard = loadDashboard;
VIEWS.studio    = loadStudio;
VIEWS.generate  = renderGenChannelPicker;
VIEWS.library   = loadLibraryData;
VIEWS.channels  = renderChannels;
VIEWS.guide     = loadGuide;
VIEWS.settings  = () => loadCfgEditor(activeCfg);

(async () => {
  channelData = await api('/channels').catch(() => []);
  loadStudio();
  loadDashboard();
  loadLibraryData();
  setInterval(() => {
    if ($('#view-studio').classList.contains('active')) {
      loadStudioExports();
      if (studioProject && !studioRenderTimer) loadStudioJobStatus();
    }
    if ($('#view-dashboard').classList.contains('active')) loadDashboard();
    if (activeJobId) {/* polling already running */}
  }, 8000);
})();
