// A plain gray field with the contract names, used until blender/field.glb lands.
// It follows the Blender rules in docs/CONTRACT.md: one parent Group per asset ID,
// children named <ID>_part, tank fill meshes named TANK_n_LEVEL with the origin at the
// tank floor and full height at scale.y = 1. 1 unit = 1 meter.
import * as THREE from "three";

const steel = () => new THREE.MeshStandardMaterial({ color: 0x8a9096, roughness: 0.6, metalness: 0.4 });
const concrete = () => new THREE.MeshStandardMaterial({ color: 0x5b6066, roughness: 0.95 });
const shellMat = () =>
  new THREE.MeshStandardMaterial({ color: 0xa4abb3, roughness: 0.4, metalness: 0.2, transparent: true, opacity: 0.18, depthWrite: false, side: THREE.DoubleSide });
const water = () => new THREE.MeshStandardMaterial({ color: 0x9aa3ab, roughness: 0.3, metalness: 0.1 });

const TANK_R = 3.4, TANK_H = 8;
const tankX = (i) => -22 + (i - 1) * 8.8;

function part(group, id, suffix, geo, mat, x, y, z) {
  const m = new THREE.Mesh(geo, mat);
  m.name = `${id}_${suffix}`;
  m.position.set(x, y, z);
  m.castShadow = m.receiveShadow = true;
  group.add(m);
  return m;
}

function asset(scene, id, x, z) {
  const g = new THREE.Group();
  g.name = id;
  g.position.set(x, 0, z);
  scene.add(g);
  return g;
}

function pipe(scene, a, b, r = 0.18) {
  const start = new THREE.Vector3(...a), end = new THREE.Vector3(...b);
  const len = start.distanceTo(end);
  const m = new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 10), steel());
  m.position.copy(start).lerp(end, 0.5);
  m.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), end.clone().sub(start).normalize());
  m.name = "pipe";
  scene.add(m);
}

export function buildPlaceholder() {
  const root = new THREE.Group();
  root.name = "placeholder_field";

  for (let i = 1; i <= 6; i++) {
    const x = tankX(i);
    // Tank: see-through shell so the fill is visible (CONTRACT rule 4).
    const t = asset(root, `TANK_${i}`, x, 0);
    part(t, `TANK_${i}`, "shell", new THREE.CylinderGeometry(TANK_R, TANK_R, TANK_H, 40, 1, true), shellMat(), 0, TANK_H / 2, 0);
    part(t, `TANK_${i}`, "roof", new THREE.CylinderGeometry(TANK_R, TANK_R, 0.15, 40), steel(), 0, TANK_H, 0).material.opacity = 1;
    part(t, `TANK_${i}`, "pad", new THREE.CylinderGeometry(TANK_R + 0.6, TANK_R + 0.6, 0.3, 40), concrete(), 0, 0.15, 0);
    const levelGeo = new THREE.CylinderGeometry(TANK_R - 0.15, TANK_R - 0.15, TANK_H, 40);
    levelGeo.translate(0, TANK_H / 2, 0); // origin at the tank floor
    const lvl = new THREE.Mesh(levelGeo, water());
    lvl.name = `TANK_${i}_LEVEL`;
    lvl.position.set(0, 0.3, 0);
    lvl.scale.y = 0.5;
    t.add(lvl);

    // Pump and its discharge valve, in front of the tank.
    const p = asset(root, `PUMP_${i}`, x, 6.5);
    part(p, `PUMP_${i}`, "skid", new THREE.BoxGeometry(2.4, 0.3, 1.6), concrete(), 0, 0.15, 0);
    part(p, `PUMP_${i}`, "motor", new THREE.CylinderGeometry(0.55, 0.55, 1.4, 20).rotateZ(Math.PI / 2), steel(), -0.4, 0.9, 0);
    part(p, `PUMP_${i}`, "volute", new THREE.CylinderGeometry(0.6, 0.6, 0.5, 20), steel(), 0.7, 0.9, 0);
    const v = asset(root, `VALVE_D_${i}`, x, 9.5);
    part(v, `VALVE_D_${i}`, "body", new THREE.SphereGeometry(0.4, 16, 12), steel(), 0, 0.6, 0);
    part(v, `VALVE_D_${i}`, "wheel", new THREE.TorusGeometry(0.45, 0.07, 8, 20).rotateX(Math.PI / 2), steel(), 0, 1.25, 0);
    pipe(root, [x, 0.6, TANK_R], [x, 0.6, 5.7]);
    pipe(root, [x, 0.6, 7.3], [x, 0.6, 9.1]);

    // Four wells per tank, behind it.
    for (let k = 0; k < 4; k++) {
      const n = (i - 1) * 4 + k + 1;
      const w = asset(root, `WELL_${n}`, x - 3 + (k % 2) * 6, -11 - Math.floor(k / 2) * 6);
      part(w, `WELL_${n}`, "pad", new THREE.BoxGeometry(2.2, 0.2, 2.2), concrete(), 0, 0.1, 0);
      part(w, `WELL_${n}`, "tree", new THREE.CylinderGeometry(0.2, 0.25, 2.2, 10), steel(), 0, 1.2, 0);
      part(w, `WELL_${n}`, "cap", new THREE.BoxGeometry(0.8, 0.3, 0.3), steel(), 0, 1.9, 0);
      pipe(root, [x - 3 + (k % 2) * 6, 0.4, -11 - Math.floor(k / 2) * 6], [x, 0.4, -TANK_R], 0.08);
    }
  }

  // Crossover between Tank 3 and Tank 4.
  const xo = asset(root, "VALVE_XO_34", (tankX(3) + tankX(4)) / 2, -1.5);
  part(xo, "VALVE_XO_34", "body", new THREE.SphereGeometry(0.5, 16, 12), steel(), 0, 0.8, 0);
  part(xo, "VALVE_XO_34", "wheel", new THREE.TorusGeometry(0.55, 0.08, 8, 20).rotateX(Math.PI / 2), steel(), 0, 1.55, 0);
  pipe(root, [tankX(3) + TANK_R, 0.8, -1.5], [tankX(4) - TANK_R, 0.8, -1.5], 0.22);

  // Disposal wells and the header to them.
  [["SWD_1", -13], ["SWD_2", 13]].forEach(([id, x]) => {
    const s = asset(root, id, x, 18);
    part(s, id, "pad", new THREE.BoxGeometry(4, 0.25, 4), concrete(), 0, 0.12, 0);
    part(s, id, "head", new THREE.CylinderGeometry(0.45, 0.45, 2.6, 16), steel(), 0, 1.4, 0);
    part(s, id, "skid", new THREE.BoxGeometry(2.4, 1.2, 1.2), steel(), 0, 0.85, 1.4);
  });
  pipe(root, [tankX(1), 0.6, 12], [tankX(6), 0.6, 12], 0.25);
  pipe(root, [-13, 0.6, 12], [-13, 0.6, 16.5], 0.25);
  pipe(root, [13, 0.6, 12], [13, 0.6, 16.5], 0.25);
  for (let i = 1; i <= 6; i++) pipe(root, [tankX(i), 0.6, 9.9], [tankX(i), 0.6, 12]);

  // Truck yard and trucks.
  const yard = asset(root, "YARD_1", 38, 14);
  part(yard, "YARD_1", "gravel", new THREE.BoxGeometry(14, 0.1, 10), concrete(), 0, 0.05, 0);
  [["TRUCK_1", 34.5], ["TRUCK_2", 40.5]].forEach(([id, x]) => {
    const tr = asset(root, id, x, 14);
    part(tr, id, "cab", new THREE.BoxGeometry(2.4, 2.2, 2.2), steel(), 0, 1.3, -3.2);
    part(tr, id, "tank", new THREE.CylinderGeometry(1.1, 1.1, 5, 20).rotateX(Math.PI / 2), steel(), 0, 1.6, 0.6);
    part(tr, id, "chassis", new THREE.BoxGeometry(2.3, 0.4, 8), concrete(), 0, 0.45, -0.6);
  });

  return root;
}
