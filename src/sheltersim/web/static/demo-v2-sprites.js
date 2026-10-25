/* Local, lazy sprite sheets. Appearance is illustrative and never inferred from traits. */
(() => {
  "use strict";
  const sheets = new Map(),
    W = 48,
    H = 64,
    FRAMES = 8;
  const skins = ["#f4c8a3", "#dba57b", "#bd815c"];
  const hair = ["#35343a", "#60453a", "#8b6750", "#555c68"];
  const pants = ["#344e67", "#51677b", "#524f61", "#405a56"];
  function rounded(c, x, y, w, h, r) {
    c.beginPath();
    c.roundRect(x, y, w, h, r);
  }
  function paint(c, variant, color, staff, direction, walking, frame) {
    const angle = (frame / FRAMES) * Math.PI * 2,
      step = walking ? Math.sin(angle) * 4 : 0;
    const bob = walking ? Math.cos(angle * 2) * 0.7 : Math.sin(angle) * 1.5;
    const skin = skins[variant % skins.length],
      back = direction === "up",
      side = direction === "left" || direction === "right";
    c.save();
    c.translate(24, 54);
    if (direction === "left") c.scale(-1, 1);
    // Boots and separated trouser legs keep the foot anchor visible.
    c.fillStyle = pants[variant % pants.length];
    rounded(c, -9, -15, 8, 16 + step, 3);
    c.fill();
    rounded(c, 1, -15, 8, 16 - step, 3);
    c.fill();
    c.fillStyle = "#243749";
    rounded(c, -10, step - 1, 10, 5, 2);
    c.fill();
    rounded(c, 0, -step - 1, 10, 5, 2);
    c.fill();
    c.translate(0, bob);
    // Soft outline, darker side seam, and collar create a compact jacket.
    c.fillStyle = "#283c5140";
    rounded(c, -14, -36, 28, 25, 8);
    c.fill();
    c.fillStyle = color;
    rounded(c, -12, -35, 24, 23, 7);
    c.fill();
    c.fillStyle = "#ffffff45";
    rounded(c, -8, -33, 7, 17, 3);
    c.fill();
    c.fillStyle = "#19344c22";
    rounded(c, 7, -32, 5, 20, 2);
    c.fill();
    c.fillStyle = color;
    rounded(c, -17, -32 - step, 7, 18, 3);
    c.fill();
    rounded(c, 10, -32 + step, 7, 18, 3);
    c.fill();
    c.fillStyle = skin;
    rounded(c, -17, -19 - step, 7, 6, 3);
    c.fill();
    rounded(c, 10, -19 + step, 7, 6, 3);
    c.fill();
    c.fillStyle = "#f7eddb";
    rounded(c, -4, -38, 8, 7, 2);
    c.fill();
    if (staff) {
      c.fillStyle = "#bceff3";
      c.fillRect(-12, -23, 24, 4);
      c.fillStyle = "#f8ffff";
      rounded(c, 3, -32, 6, 7, 1);
      c.fill();
      c.fillStyle = "#177bba";
      c.fillRect(5, -30, 2, 3);
    } else if (variant % 3 === 0) {
      c.fillStyle = "#ffffff83";
      c.fillRect(-11, -25, 22, 2);
      c.fillRect(-11, -21, 22, 2);
    } else if (variant % 3 === 1) {
      c.strokeStyle = "#ffffff70";
      c.lineWidth = 1.2;
      c.beginPath();
      c.moveTo(0, -32);
      c.lineTo(0, -13);
      c.stroke();
    }
    // Large head, ear, face and hair silhouette remain legible at map scale.
    c.fillStyle = "#21394c22";
    c.beginPath();
    c.ellipse(0.5, -42, 13.5, 13, 0, 0, Math.PI * 2);
    c.fill();
    c.fillStyle = skin;
    c.beginPath();
    c.ellipse(0, -43, 12.2, 12.5, 0, 0, Math.PI * 2);
    c.fill();
    c.beginPath();
    c.arc(side ? 11 : -11, -42, 3, 0, Math.PI * 2);
    c.fill();
    c.fillStyle = hair[variant % hair.length];
    c.beginPath();
    c.ellipse(-1, -48, 12.6, 9, 0, Math.PI, Math.PI * 2);
    c.lineTo(11, -44);
    c.lineTo(7, -48);
    c.lineTo(3, -46);
    c.lineTo(-3, -49);
    c.lineTo(-11, -43);
    c.closePath();
    c.fill();
    if (back) {
      c.beginPath();
      c.ellipse(0, -45, 11.8, 10.7, 0, 0, Math.PI * 2);
      c.fill();
    } else {
      c.fillStyle = "#2d3944";
      if (side) {
        rounded(c, 5, -44, 2, 3, 1);
        c.fill();
        c.fillStyle = skin;
        c.fillRect(11, -42, 3, 3);
      } else {
        rounded(c, -6, -44, 2.3, 3, 1);
        c.fill();
        rounded(c, 4, -44, 2.3, 3, 1);
        c.fill();
      }
      c.strokeStyle = "#a96354";
      c.lineWidth = 1;
      c.beginPath();
      c.arc(side ? 7 : 0, -39, 2.5, 0, Math.PI * 0.75);
      c.stroke();
    }
    if (staff) {
      c.fillStyle = "#e4f9fb";
      c.beginPath();
      c.ellipse(0, -52, 13, 5, 0, Math.PI, Math.PI * 2);
      c.fill();
      c.fillStyle = "#178ec5";
      rounded(c, -14, -51, 28, 3, 1);
      c.fill();
    }
    c.restore();
  }
  function sheet(variant, color, staff, direction, walking) {
    const key = [variant, color, staff, direction, walking].join(":");
    if (sheets.has(key)) return sheets.get(key);
    const canvas = document.createElement("canvas");
    canvas.width = W * FRAMES;
    canvas.height = H;
    const c = canvas.getContext("2d");
    for (let i = 0; i < FRAMES; i++) {
      c.save();
      c.translate(i * W, 0);
      paint(c, variant, color, staff, direction, walking, i);
      c.restore();
    }
    if (sheets.size >= 384) sheets.delete(sheets.keys().next().value);
    sheets.set(key, canvas);
    return canvas;
  }
  function draw(ctx, x, y, size, color, staff, selected, motion = {}) {
    const direction = motion.directionName || "down";
    const variant = (motion.variant || 0) % 12,
      walking = Boolean(motion.walking);
    const phase = walking
      ? (motion.distance || 0) / Math.max(size * 3.4, 1)
      : (motion.time || 0) / 3800 + variant * 0.13;
    const frame = ((Math.floor(phase * FRAMES) % FRAMES) + FRAMES) % FRAMES;
    ctx.save();
    ctx.fillStyle = "#233c543b";
    ctx.beginPath();
    ctx.ellipse(x, y + 1, size * 0.9, size * 0.28, 0, 0, Math.PI * 2);
    ctx.fill();
    if (selected) {
      ctx.fillStyle = "#00b3a127";
      ctx.strokeStyle = "#00a894";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.ellipse(x, y + 1, size * 1.35, size * 0.48, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
    const image = sheet(variant, color, staff, direction, walking);
    const width = size * 2.6,
      height = size * 3.47;
    ctx.drawImage(
      image,
      frame * W,
      0,
      W,
      H,
      x - width / 2,
      y - (height * 54) / H,
      width,
      height,
    );
    if (motion.queued) {
      ctx.fillStyle = "#ffb225";
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(x + size * 0.9, y - size * 1.8, 2.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
    if (selected && motion.label) {
      ctx.font = "700 10px sans-serif";
      const width = ctx.measureText(motion.label).width + 14;
      ctx.fillStyle = "#ffffffed";
      ctx.strokeStyle = "#14af9e";
      ctx.lineWidth = 1;
      rounded(ctx, x - width / 2, y - height - 11, width, 18, 5);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = "#163c67";
      ctx.textAlign = "center";
      ctx.fillText(motion.label, x, y - height + 1);
    }
    ctx.restore();
    return { frame, direction, walking };
  }
  const direction = (angle) =>
    Math.abs(Math.cos(angle)) > Math.abs(Math.sin(angle))
      ? Math.cos(angle) >= 0
        ? "right"
        : "left"
      : Math.sin(angle) >= 0
        ? "down"
        : "up";
  window.HinanjoSprites = { draw, direction, cacheSize: () => sheets.size };
})();
