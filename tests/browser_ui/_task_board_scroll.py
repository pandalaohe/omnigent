"""Measure source rows after browser scrolling and layout have settled."""

from playwright.sync_api import Locator, Page


def wait_for_history_list(page: Page, count: int) -> dict:
    return page.evaluate(
        """async count => {
          await document.fonts.ready;
          let previous = '', stable = 0;
          const deadline = performance.now() + 5000;
          return new Promise((resolve, reject) => {
            const sample = () => {
              const list = document.querySelector('.h-list');
              const rows = document.querySelectorAll('.h-p').length;
              const r = list?.getBoundingClientRect();
              const box = r && {x:r.x, y:r.y, width:r.width, height:r.height};
              const current = JSON.stringify({rows, box});
              stable = rows === count && r?.width > 0 && r?.height > 0 &&
                current === previous ? stable + 1 : 0;
              previous = current;
              if (stable >= 2) resolve({rows, box, stableFrames:stable});
              else if (performance.now() >= deadline) reject(new Error(
                'History list did not settle: ' + current));
              else requestAnimationFrame(sample);
            };
            requestAnimationFrame(sample);
          });
        }""",
        count,
    )


def scroll_to_history_row(row: Locator) -> dict:
    return row.evaluate(
        """async el => {
          const fontStatus = document.fonts.status;
          await document.fonts.ready;
          const index = [...document.querySelectorAll('.h-p')].indexOf(el);
          const name = el.querySelector('.h-name b')?.textContent;
          let replacements = 0;
          const scrollers = () => {
            const parents = [];
            for (let parent = el.parentElement; parent; parent = parent.parentElement) {
              if (parent.scrollHeight > parent.clientHeight) parents.push(parent);
            }
            return parents;
          };
          const sample = () => {
            const r = el.getBoundingClientRect();
            const hit = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
            return {
              box: {x:r.x, y:r.y, width:r.width, height:r.height},
              scroll: scrollers().map(parent => [parent.scrollLeft, parent.scrollTop]),
              hit: !!hit && el.contains(hit),
              hitElement: hit ? hit.tagName + '.' + hit.className : null,
              inside: r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= innerHeight,
            };
          };
          const before = sample();
          el.scrollIntoView({block:'center'});
          const immediate = sample();
          const scrollBehavior = scrollers().map(
            parent => getComputedStyle(parent).scrollBehavior);
          let previous = immediate, stable = 0, frames = 0;
          const samples = [], deadline = performance.now() + 5000;
          return new Promise((resolve, reject) => {
            const check = () => {
              // Responsive redraws can replace the row while scrolling is in flight.
              const live = document.querySelectorAll('.h-p')[index];
              if (live && live !== el && live.querySelector('.h-name b')?.textContent === name) {
                el = live;
                replacements++;
                stable = 0;
                el.scrollIntoView({block:'center'});
              }
              const current = sample();
              frames++;
              const positions = value => [...Object.values(value.box), ...value.scroll.flat()];
              const priorPositions = positions(previous), currentPositions = positions(current);
              const unchanged = currentPositions.every((value, i) =>
                Math.abs(value - priorPositions[i]) < 0.5);
              stable = unchanged && current.inside && current.hit ? stable + 1 : 0;
              if (samples.length < 5 || JSON.stringify(current) !== JSON.stringify(previous)) {
                samples.push(current);
              }
              previous = current;
              const result = {
                name, replacements, fontStatus, scrollBehavior,
                before, immediate, frames, samples, settled:current,
              };
              if (stable >= 3) resolve(result);
              else if (performance.now() >= deadline) reject(new Error(
                'History row did not settle inside viewport with its centre exposed: ' +
                JSON.stringify(result)));
              else requestAnimationFrame(check);
            };
            requestAnimationFrame(check);
          });
        }"""
    )
