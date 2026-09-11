(() => {
  const DATA_URL = 'events.json';
  const PARIS_TZ = 'Europe/Paris';

  const cards = {
    4: document.querySelector('#card-thursday'),
    5: document.querySelector('#card-friday'),
    6: document.querySelector('#card-saturday')
  };

  const labels = {
    4: 'JEUDI',
    5: 'VENDREDI',
    6: 'SAMEDI'
  };

  function parisParts(date) {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: PARIS_TZ,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      weekday: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    }).formatToParts(date);

    return Object.fromEntries(parts.map(part => [part.type, part.value]));
  }

  function weekdayNumber(date) {
    const day = new Intl.DateTimeFormat('en-US', {
      timeZone: PARIS_TZ,
      weekday: 'short'
    }).format(date);

    return ({ Thu: 4, Fri: 5, Sat: 6 })[day] || 0;
  }

  function formatDate(date) {
    const p = parisParts(date);
    return `${p.day}.${p.month}.${String(p.year).slice(-2)}`;
  }

  function formatTimeRange(start, end) {
    const startParts = parisParts(start);
    const startText = `${startParts.hour}H${startParts.minute === '00' ? '' : startParts.minute}`;

    if (!end) {
      return startText;
    }

    const endParts = parisParts(end);
    const endText = `${endParts.hour}H${endParts.minute === '00' ? '' : endParts.minute}`;
    return `${startText} — ${endText}`;
  }

  function eventEnd(event, start) {
    if (event.end_time) {
      const end = new Date(event.end_time);
      if (!Number.isNaN(end.getTime())) return end;
    }

    return new Date(start.getTime() + 8 * 60 * 60 * 1000);
  }

  function resetCard(card, label) {
    if (!card) return;

    card.querySelector('.event-date').textContent = '—';
    card.querySelector('.event-title').textContent = 'Programme à venir';
    card.querySelector('.event-type').textContent = 'À annoncer';
    card.querySelector('.placeholder-date').textContent = label;
    card.querySelector('.placeholder-title').textContent = 'PROGRAMME À VENIR';
    card.querySelector('.lineup').innerHTML = '';

    const flyer = card.querySelector('.event-flyer');
    flyer.hidden = true;
    flyer.removeAttribute('src');
    flyer.style.objectFit = '';
    flyer.style.background = '';

    const button = card.querySelector('.shotgun-button');
    button.hidden = true;
    button.removeAttribute('href');
  }

  function renderCard(card, event, label) {
    if (!card || !event) {
      resetCard(card, label);
      return;
    }

    const start = new Date(event.start_time);
    const end = event.end_time ? new Date(event.end_time) : null;

    card.querySelector('.event-date').textContent = formatDate(start);
    card.querySelector('.event-title').textContent = event.name;
    card.querySelector('.event-type').textContent = formatTimeRange(start, end);
    card.querySelector('.placeholder-date').textContent = `${label} ${formatDate(start)}`;
    card.querySelector('.placeholder-title').textContent = event.name;
    card.querySelector('.lineup').innerHTML = '';

    const flyer = card.querySelector('.event-flyer');
    const hasPortrait = Boolean(event.portrait_url);
    const flyerUrl = event.portrait_url || event.cover_url;

    if (flyerUrl) {
      flyer.src = flyerUrl;
      flyer.alt = `${event.name} — Danceteria`;
      flyer.hidden = false;

      // The Shotgun Events API currently exposes the 16:9 cover. Until the
      // portrait asset is available, show the entire banner rather than
      // brutally cropping titles inside the site's 4:5 card.
      flyer.style.objectFit = hasPortrait ? 'cover' : 'contain';
      flyer.style.background = '#050505';

      flyer.onerror = () => {
        if (event.portrait_url && event.cover_url && flyer.src !== event.cover_url) {
          flyer.src = event.cover_url;
          flyer.style.objectFit = 'contain';
          flyer.style.background = '#050505';
          return;
        }
        flyer.hidden = true;
      };
    } else {
      flyer.hidden = true;
      flyer.removeAttribute('src');
      flyer.style.objectFit = '';
      flyer.style.background = '';
    }

    const button = card.querySelector('.shotgun-button');
    if (event.url) {
      button.href = event.url;
      button.hidden = false;
    } else {
      button.hidden = true;
      button.removeAttribute('href');
    }
  }

  function chooseCurrentThenNextByWeekday(events, weekday) {
    const now = new Date();

    return events
      .map(event => {
        const start = new Date(event.start_time);
        return {
          ...event,
          _start: start,
          _end: eventEnd(event, start)
        };
      })
      .filter(event => !Number.isNaN(event._start.getTime()))
      .filter(event => !Number.isNaN(event._end.getTime()))
      .filter(event => weekdayNumber(event._start) === weekday)
      // An event remains on the card until its actual Shotgun end time.
      // Only after that moment may the next event for this weekday replace it.
      .filter(event => event._end > now)
      .sort((a, b) => a._start - b._start)[0] || null;
  }

  async function initialiseShotgunAgenda() {
    try {
      const response = await fetch(`${DATA_URL}?t=${Date.now()}`, {
        cache: 'no-store'
      });

      if (!response.ok) {
        throw new Error(`events.json ${response.status}`);
      }

      const payload = await response.json();
      const events = Array.isArray(payload.events) ? payload.events : [];

      [4, 5, 6].forEach(weekday => {
        renderCard(
          cards[weekday],
          chooseCurrentThenNextByWeekday(events, weekday),
          labels[weekday]
        );
      });
    } catch (error) {
      console.error('Impossible de charger l’agenda Shotgun.', error);
      [4, 5, 6].forEach(weekday => resetCard(cards[weekday], labels[weekday]));
    }
  }

  initialiseShotgunAgenda();
})();
