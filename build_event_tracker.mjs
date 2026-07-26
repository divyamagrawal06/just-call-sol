import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputDir = path.resolve("outputs", "sarvam_epoch_event_tracker");
await fs.mkdir(outputDir, { recursive: true });

const headers = [
  "Registration ID",
  "Full Name",
  "Email",
  "Phone",
  "Ticket Type",
  "Organization",
  "Role / Designation",
  "Registered At",
  "Payment Status",
  "Government ID Check",
  "Waiver & Code of Conduct",
  "Duplicate Registration Check",
  "Approval Status",
  "Approval Reason",
  "Check-in Status",
  "Gate / Coordinator Note",
];

const people = [
  ["SEP-26001","Aarav Sharma","aarav.sharma@epoch-attendee.example","+91-90000-10001","General","NimbleStack Labs","ML Engineer","2026-07-02 09:14","Paid","Verified","Accepted","Clear","APPROVED","Payment received; identity verified; required waiver accepted.","Not Checked In",""],
  ["SEP-26002","Meera Iyer","meera.iyer@epoch-attendee.example","+91-90000-10002","VIP","InnoWave Systems","Product Lead","2026-07-02 09:32","Paid","Verified","Accepted","Clear","APPROVED","VIP allocation confirmed; payment and identity checks complete.","Checked In","Badge issued at 08:41 by Desk A."],
  ["SEP-26003","Kabir Malhotra","kabir.malhotra@epoch-attendee.example","+91-90000-10003","General","Freelance","AI Consultant","2026-07-02 10:05","Paid","Mismatch","Accepted","Clear","REJECTED","Name on submitted ID does not match the registration; manual review was not completed before the cutoff.","Denied at Gate","Escalate only to Registration Lead; do not admit on payment receipt alone."],
  ["SEP-26004","Ananya Rao","ananya.rao@epoch-attendee.example","+91-90000-10004","Student","IIT Hyderabad","Student","2026-07-02 10:28","Paid","Verified","Accepted","Clear","APPROVED","Student credential, payment, and identity verification complete.","Not Checked In",""],
  ["SEP-26005","Vihaan Gupta","vihaan.gupta@epoch-attendee.example","+91-90000-10005","General","VectorMint","Data Scientist","2026-07-02 11:01","Failed","Verified","Accepted","Clear","PENDING","Payment attempt failed; approval will be issued only after successful payment.","Hold at Gate","Direct participant to payments help desk."],
  ["SEP-26006","Sana Khan","sana.khan@epoch-attendee.example","+91-90000-10006","Speaker","OpenCompute Collective","Researcher","2026-07-02 11:47","Waived","Verified","Accepted","Clear","APPROVED","Speaker registration confirmed; fee waived under speaker policy; identity verified.","Checked In","Green room access enabled."],
  ["SEP-26007","Rohan Mehta","rohan.mehta@epoch-attendee.example","+91-90000-10007","General","CloudCraft India","Solutions Architect","2026-07-03 08:12","Paid","Verified","Accepted","Duplicate Found","REJECTED","Duplicate registration detected under SEP-26019; this record was voided to prevent a second badge.","Denied at Gate","Use SEP-26019 if participant presents matching photo ID."],
  ["SEP-26008","Ishita Nair","ishita.nair@epoch-attendee.example","+91-90000-10008","General","QuantumLeaf","UX Researcher","2026-07-03 08:39","Paid","Verified","Accepted","Clear","APPROVED","All mandatory registration checks passed.","Not Checked In",""],
  ["SEP-26009","Arjun Bansal","arjun.bansal@epoch-attendee.example","+91-90000-10009","Workshop","DataForge","Backend Engineer","2026-07-03 09:03","Paid","Verified","Accepted","Clear","APPROVED","Workshop seat confirmed; payment, waiver, and identity checks complete.","Checked In","Workshop band issued."],
  ["SEP-26010","Diya Menon","diya.menon@epoch-attendee.example","+91-90000-10010","Student","BITS Pilani","Student","2026-07-03 09:24","Refunded","Verified","Accepted","Clear","CANCELLED","Participant cancelled the registration; payment was refunded on 2026-07-18.","Not Checked In","Do not issue badge unless Registration Lead creates a new record."],
  ["SEP-26011","Aditya Kulkarni","aditya.kulkarni@epoch-attendee.example","+91-90000-10011","General","SignalNest","Founder","2026-07-03 10:10","Paid","Verified","Accepted","Clear","APPROVED","Payment and all compliance checks complete.","Not Checked In",""],
  ["SEP-26012","Nisha Verma","nisha.verma@epoch-attendee.example","+91-90000-10012","General","BrightLoop","Engineering Manager","2026-07-03 10:46","Paid","Pending","Accepted","Clear","PENDING","Government ID review is still pending; gate admission is not authorized.","Hold at Gate","Send to identity verification desk with original photo ID."],
  ["SEP-26013","Reyansh Patel","reyansh.patel@epoch-attendee.example","+91-90000-10013","VIP","SynapseWorks","CTO","2026-07-03 11:31","Paid","Verified","Accepted","Clear","APPROVED","VIP invitation matched; payment and identity checks complete.","Not Checked In",""],
  ["SEP-26014","Kavya Singh","kavya.singh@epoch-attendee.example","+91-90000-10014","Workshop","ModelMosaic","Prompt Engineer","2026-07-03 12:02","Paid","Verified","Accepted","Clear","WAITLISTED","Mandatory checks passed, but the workshop capacity is full; no admission until a seat is released.","Hold at Gate","General expo access is not included in this workshop-only ticket."],
  ["SEP-26015","Dev Joshi","dev.joshi@epoch-attendee.example","+91-90000-10015","General","CodeHarbor","DevOps Engineer","2026-07-04 08:20","Paid","Verified","Accepted","Clear","APPROVED","All mandatory registration checks passed.","Checked In","Badge issued at 08:55 by Desk B."],
  ["SEP-26016","Priya Desai","priya.desai@epoch-attendee.example","+91-90000-10016","Media","TechWire India","Correspondent","2026-07-04 08:52","Waived","Verified","Accepted","Clear","APPROVED","Media accreditation approved; identity and code-of-conduct checks complete.","Not Checked In","Collect media badge at PR desk."],
  ["SEP-26017","Neil Chatterjee","neil.chatterjee@epoch-attendee.example","+91-90000-10017","General","KernelNine","Security Analyst","2026-07-04 09:17","Paid","Verified","Not Accepted","Clear","REJECTED","Required event waiver and code of conduct were not accepted by the deadline.","Denied at Gate","Participant may not sign retroactively at the gate without Registration Lead approval."],
  ["SEP-26018","Tanya Kapoor","tanya.kapoor@epoch-attendee.example","+91-90000-10018","Student","Delhi Technological University","Student","2026-07-04 09:43","Paid","Verified","Accepted","Clear","APPROVED","Student credential and all mandatory checks passed.","Not Checked In",""],
  ["SEP-26019","Rohan Mehta","rohan.mehta@epoch-attendee.example","+91-90000-10007","General","CloudCraft India","Solutions Architect","2026-07-04 10:06","Paid","Verified","Accepted","Clear","APPROVED","Canonical registration retained after duplicate review; payment and identity verified.","Not Checked In","Admit only against this registration ID; void record SEP-26007."],
  ["SEP-26020","Fatima Sheikh","fatima.sheikh@epoch-attendee.example","+91-90000-10020","General","CivicAI Foundation","Program Manager","2026-07-04 10:34","Paid","Verified","Accepted","Clear","APPROVED","All mandatory registration checks passed.","Checked In","Accessibility seating requested and reserved."],
  ["SEP-26021","Kunal Sethi","kunal.sethi@epoch-attendee.example","+91-90000-10021","VIP","OmniScale","VP Engineering","2026-07-04 11:08","Chargeback","Verified","Accepted","Clear","REJECTED","Payment was reversed by the card issuer; registration approval was withdrawn.","Denied at Gate","Refer payment disputes to Finance Desk."],
  ["SEP-26022","Riya Bose","riya.bose@epoch-attendee.example","+91-90000-10022","General","PixelRoute","Interaction Designer","2026-07-04 11:55","Paid","Verified","Accepted","Clear","APPROVED","All mandatory registration checks passed.","Not Checked In",""],
  ["SEP-26023","Siddharth Jain","siddharth.jain@epoch-attendee.example","+91-90000-10023","Workshop","NeuralFoundry","Applied Scientist","2026-07-05 08:11","Paid","Verified","Accepted","Clear","APPROVED","Workshop seat and all mandatory checks confirmed.","Not Checked In","Bring laptop for hands-on lab."],
  ["SEP-26024","Aisha Thomas","aisha.thomas@epoch-attendee.example","+91-90000-10024","General","GreenCircuit","Sustainability Lead","2026-07-05 08:40","Paid","Expired","Accepted","Clear","PENDING","Submitted ID expired before the event date; replacement document is required.","Hold at Gate","Identity desk may approve after checking a valid original ID."],
  ["SEP-26025","Manav Arora","manav.arora@epoch-attendee.example","+91-90000-10025","Student","IIIT Bangalore","Student","2026-07-05 09:06","Paid","Verified","Accepted","Clear","APPROVED","Student credential and all mandatory checks passed.","Checked In","Badge issued at 09:02 by Desk C."],
  ["SEP-26026","Shruti Reddy","shruti.reddy@epoch-attendee.example","+91-90000-10026","Speaker","Responsible AI Forum","Policy Fellow","2026-07-05 09:38","Waived","Verified","Accepted","Clear","APPROVED","Speaker registration confirmed; identity and compliance checks complete.","Not Checked In","Speaker escort requested for Hall 2."],
  ["SEP-26027","Yash Tandon","yash.tandon@epoch-attendee.example","+91-90000-10027","General","ByteBanyan","Full-stack Developer","2026-07-05 10:13","Processing","Verified","Accepted","Clear","PENDING","Payment is still processing; no settled transaction is recorded.","Hold at Gate","Check payment dashboard before escalation."],
  ["SEP-26028","Leena Dutta","leena.dutta@epoch-attendee.example","+91-90000-10028","General","InsightArc","Business Analyst","2026-07-05 10:49","Paid","Verified","Accepted","Clear","APPROVED","All mandatory registration checks passed.","Not Checked In",""],
  ["SEP-26029","Harsh Vardhan","harsh.vardhan@epoch-attendee.example","+91-90000-10029","Workshop","AutoGraph Labs","Robotics Engineer","2026-07-05 11:22","Paid","Verified","Accepted","Clear","WAITLISTED","Registration is valid, but the workshop remains at capacity.","Not Checked In","Admit only after status changes to APPROVED."],
  ["SEP-26030","Zoya Mirza","zoya.mirza@epoch-attendee.example","+91-90000-10030","Media","FutureNow","Video Producer","2026-07-05 11:58","Waived","Verified","Accepted","Clear","APPROVED","Media accreditation, identity, and conduct checks complete.","Checked In","Camera equipment tagged at security."],
  ["SEP-26031","Om Prakash","om.prakash@epoch-attendee.example","+91-90000-10031","General","Indie Developer","Developer","2026-07-06 08:16","Paid","Verified","Accepted","Clear","APPROVED","All mandatory registration checks passed.","Not Checked In",""],
  ["SEP-26032","Maya Krishnan","maya.krishnan@epoch-attendee.example","+91-90000-10032","VIP","LatticeMind","CEO","2026-07-06 08:47","Paid","Verified","Accepted","Clear","APPROVED","VIP invitation and all mandatory checks confirmed.","Checked In","VIP lounge access enabled."],
  ["SEP-26033","Parth Trivedi","parth.trivedi@epoch-attendee.example","+91-90000-10033","General","ComputeSpring","Data Engineer","2026-07-06 09:21","Refunded","Verified","Accepted","Clear","CANCELLED","Registration was cancelled by the participant and fully refunded.","Not Checked In",""],
  ["SEP-26034","Neha Pillai","neha.pillai@epoch-attendee.example","+91-90000-10034","Student","NIT Calicut","Student","2026-07-06 09:59","Paid","Verified","Accepted","Clear","APPROVED","Student credential and all mandatory checks passed.","Not Checked In",""],
  ["SEP-26035","Sameer Qureshi","sameer.qureshi@epoch-attendee.example","+91-90000-10035","General","AudioLens AI","Research Engineer","2026-07-06 10:33","Paid","Verified","Accepted","Clear","APPROVED","All mandatory registration checks passed.","Not Checked In",""],
  ["SEP-26036","Jhanvi Shah","jhanvi.shah@epoch-attendee.example","+91-90000-10036","Workshop","ProtoVerse","Product Designer","2026-07-06 11:07","Paid","Verified","Accepted","Clear","APPROVED","Workshop seat and all mandatory checks confirmed.","Not Checked In",""],
];

function escapeCsv(value) {
  const text = String(value ?? "");
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

const csvText = [headers, ...people]
  .map((row) => row.map(escapeCsv).join(","))
  .join("\r\n") + "\r\n";

const csvPath = path.join(outputDir, "sarvam_epoch_registration_tracker.csv");
await fs.writeFile(csvPath, "\uFEFF" + csvText, "utf8");

const workbook = await Workbook.fromCSV(csvText, { sheetName: "Registrations" });
const sheet = workbook.worksheets.getItem("Registrations");
sheet.showGridLines = false;
sheet.freezePanes.freezeRows(1);
sheet.getRange("A1:P37").format = {
  font: { name: "Aptos", size: 10, color: "#172033" },
  verticalAlignment: "center",
};
sheet.getRange("A1:P1").format = {
  fill: "#172554",
  font: { name: "Aptos Display", size: 10, bold: true, color: "#FFFFFF" },
  wrapText: true,
  rowHeight: 34,
  verticalAlignment: "center",
};
sheet.getRange("A2:P37").format.borders = {
  insideHorizontal: { style: "thin", color: "#E2E8F0" },
};
sheet.getRange("A2:A37").format.font = { bold: true, color: "#1D4ED8" };
sheet.getRange("M2:M37").conditionalFormats.add("containsText", {
  text: "APPROVED",
  format: { fill: "#DCFCE7", font: { bold: true, color: "#166534" } },
});
sheet.getRange("M2:M37").conditionalFormats.add("containsText", {
  text: "REJECTED",
  format: { fill: "#FEE2E2", font: { bold: true, color: "#991B1B" } },
});
sheet.getRange("M2:M37").conditionalFormats.add("containsText", {
  text: "PENDING",
  format: { fill: "#FEF3C7", font: { bold: true, color: "#92400E" } },
});
sheet.getRange("M2:M37").conditionalFormats.add("containsText", {
  text: "WAITLISTED",
  format: { fill: "#E0E7FF", font: { bold: true, color: "#3730A3" } },
});
sheet.getRange("M2:M37").conditionalFormats.add("containsText", {
  text: "CANCELLED",
  format: { fill: "#E5E7EB", font: { bold: true, color: "#374151" } },
});
const widths = [15,18,34,18,14,24,22,19,17,20,24,24,17,54,18,52];
for (let i = 0; i < widths.length; i++) {
  sheet.getRangeByIndexes(0, i, 37, 1).format.columnWidth = widths[i];
}
sheet.getRange("N2:P37").format.wrapText = true;
sheet.getRange("A2:P37").format.rowHeight = 30;
sheet.tables.add("A1:P37", true, "SarvamEpochRegistrations").style = "TableStyleMedium2";

const inspect = await workbook.inspect({
  kind: "table",
  range: "Registrations!A1:P8",
  include: "values,formulas",
  tableMaxRows: 8,
  tableMaxCols: 16,
});
console.log(inspect.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});
console.log(errors.ndjson);

const preview = await workbook.render({
  sheetName: "Registrations",
  range: "A1:P12",
  scale: 1,
  format: "png",
});
await fs.writeFile(
  path.join(outputDir, "registration_tracker_preview.png"),
  new Uint8Array(await preview.arrayBuffer()),
);

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(path.join(outputDir, "sarvam_epoch_registration_tracker.xlsx"));

console.log(JSON.stringify({ csvPath, rows: people.length, columns: headers.length }));
