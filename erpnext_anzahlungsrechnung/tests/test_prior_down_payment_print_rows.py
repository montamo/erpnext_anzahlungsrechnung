# Copyright (c) 2026, ALYF GmbH and Contributors
# See license.txt

from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from erpnext_anzahlungsrechnung.scripts.print_and_e_invoice_utils import (
	_build_allocated_print_rows,
	get_final_invoice_prior_down_payments,
)
from erpnext_anzahlungsrechnung.scripts.sales_invoice import (
	get_prior_down_payment_invoices_for_final_invoice,
)


class TestPriorDownPaymentPrintRows(UnitTestCase):
	def _totals(self, rows):
		return [
			{
				"invoice_no": invoice_no,
				"invoice_date": None,
				"grand_total": amount,
			}
			for invoice_no, amount in rows
		]

	def _payments(self, rows):
		return [
			{
				"pe": pe,
				"amount": amount,
				"payment_date": payment_date,
				"posting_date": posting_date,
			}
			for pe, amount, payment_date, posting_date in rows
		]

	def test_spec_example_assigns_by_payment_date(self):
		totals = self._totals([("DPI-1", 1000), ("DPI-2", 1000)])
		dpi_dates = [date(2026, 4, 1), date(2026, 4, 16)]
		payments = self._payments(
			[
				("PE-1", 400, date(2026, 4, 2), date(2026, 4, 2)),
				("PE-2", 400, date(2026, 4, 15), date(2026, 4, 15)),
				("PE-3", 1000, date(2026, 4, 16), date(2026, 4, 16)),
			]
		)

		rows = _build_allocated_print_rows(totals, dpi_dates, payments, 2)

		self.assertEqual(
			[(r["invoice_no"], r["payment_date"], r["paid_amount"]) for r in rows],
			[
				("DPI-1", date(2026, 4, 2), 400),
				("DPI-1", date(2026, 4, 15), 400),
				("DPI-2", date(2026, 4, 16), 1000),
			],
		)

	def test_same_dpi_date_prefers_later_invoice_before_earlier(self):
		totals = self._totals([("AZ-RE10069", 11144.64), ("AZ-RE10070", 1082.81)])
		dpi_dates = [date(2026, 3, 31), date(2026, 3, 31)]
		payments = self._payments(
			[
				("PE-1", 1082.81, date(2026, 4, 8), date(2026, 3, 31)),
				("PE-2", 11144.64, date(2026, 4, 8), date(2026, 6, 9)),
			]
		)

		rows = _build_allocated_print_rows(totals, dpi_dates, payments, 2)

		self.assertEqual(
			[(r["invoice_no"], r["payment_date"], r["paid_amount"]) for r in rows],
			[
				("AZ-RE10070", date(2026, 4, 8), 1082.81),
				("AZ-RE10069", date(2026, 4, 8), 11144.64),
			],
		)

	def test_overflow_spills_to_earlier_invoice_when_later_is_paid(self):
		totals = self._totals([("DPI-1", 1000), ("DPI-2", 1000)])
		dpi_dates = [date(2026, 4, 1), date(2026, 4, 16)]
		payments = self._payments(
			[
				("PE-1", 1200, date(2026, 4, 16), date(2026, 4, 16)),
			]
		)

		rows = _build_allocated_print_rows(totals, dpi_dates, payments, 2)

		self.assertEqual(
			[(r["invoice_no"], r["paid_amount"]) for r in rows],
			[
				("DPI-2", 1000),
				("DPI-1", 200),
			],
		)

	def test_unpaid_invoice_gets_placeholder_row(self):
		totals = self._totals([("DPI-1", 1000), ("DPI-2", 1000)])
		dpi_dates = [date(2026, 4, 1), date(2026, 4, 16)]
		payments = self._payments(
			[
				("PE-1", 400, date(2026, 4, 2), date(2026, 4, 2)),
			]
		)

		rows = _build_allocated_print_rows(totals, dpi_dates, payments, 2)

		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0]["invoice_no"], "DPI-1")
		self.assertEqual(rows[0]["paid_amount"], 400)
		self.assertEqual(rows[1]["invoice_no"], "DPI-2")
		self.assertIsNone(rows[1]["payment_date"])
		self.assertIsNone(rows[1]["paid_amount"])

	def test_taxes_shown_only_on_first_row_when_pe_splits(self):
		totals = self._totals([("DPI-1", 1000), ("DPI-2", 1000)])
		dpi_dates = [date(2026, 4, 1), date(2026, 4, 16)]
		payments = self._payments(
			[
				("PE-1", 1200, date(2026, 4, 16), date(2026, 4, 16)),
			]
		)
		pe_taxes = {
			"PE-1": [
				{"description": "Umsatzsteuer 19%", "amount": 95},
				{"description": "Umsatzsteuer 7%", "amount": 35},
			],
		}

		rows = _build_allocated_print_rows(totals, dpi_dates, payments, 2, pe_taxes)

		self.assertEqual(rows[0]["taxes"], pe_taxes["PE-1"])
		self.assertIsNone(rows[1]["taxes"])


class TestPriorDownPaymentInvoiceLookup(UnitTestCase):
	def _final_invoice_doc(self, **kwargs):
		defaults = {
			"doctype": "Sales Invoice",
			"name": "SINV-1",
			"custom_invoice_type": "Final Invoice",
			"customer": "CUS-1",
			"company": "Company 1",
			"project": "PROJ-1",
			"items": [frappe._dict({"sales_order": "SO-NEW"})],
		}
		defaults.update(kwargs)
		return frappe._dict(defaults)

	def test_exact_sales_order_match_is_preferred(self):
		dpi = frappe._dict(
			{
				"name": "AZ-EXACT",
				"posting_date": date(2026, 6, 1),
				"down_payment_amount": 100,
				"sales_order": "SO-NEW",
			}
		)
		with patch(
			"erpnext_anzahlungsrechnung.scripts.sales_invoice.frappe.get_all",
			return_value=[dpi],
		) as mock_get_all:
			rows = get_prior_down_payment_invoices_for_final_invoice(self._final_invoice_doc())

		self.assertEqual([row.name for row in rows], ["AZ-EXACT"])
		mock_get_all.assert_called_once()

	def test_project_fallback_finds_down_payment_invoice_from_recreated_sales_order(self):
		old_dpi = frappe._dict(
			{
				"name": "AZ-OLD",
				"posting_date": date(2026, 6, 1),
				"down_payment_amount": 100,
				"sales_order": "SO-OLD",
			}
		)
		other_project_dpi = frappe._dict(
			{
				"name": "AZ-OTHER",
				"posting_date": date(2026, 6, 2),
				"down_payment_amount": 200,
				"sales_order": "SO-OTHER",
			}
		)

		def fake_get_all(doctype, filters=None, fields=None, order_by=None):
			if filters and filters.get("sales_order"):
				return []
			return [old_dpi, other_project_dpi]

		def fake_get_value(doctype, name, fieldname):
			if doctype == "Sales Order" and fieldname == "project":
				return {"SO-OLD": "PROJ-1", "SO-OTHER": "PROJ-2"}.get(name)
			return None

		with (
			patch(
				"erpnext_anzahlungsrechnung.scripts.sales_invoice.frappe.get_all",
				side_effect=fake_get_all,
			),
			patch(
				"erpnext_anzahlungsrechnung.scripts.sales_invoice.frappe.db.get_value",
				side_effect=fake_get_value,
			),
			patch(
				"erpnext_anzahlungsrechnung.scripts.sales_invoice._down_payment_invoice_has_custom_project",
				return_value=False,
			),
		):
			rows = get_prior_down_payment_invoices_for_final_invoice(self._final_invoice_doc())

		self.assertEqual([row.name for row in rows], ["AZ-OLD"])

	def test_print_helper_uses_lookup_fallback_when_child_table_is_empty(self):
		doc = self._final_invoice_doc(custom_down_payments=[], advances=[])
		doc.precision = lambda fieldname: 2
		dpi = frappe._dict(
			{
				"name": "AZ-OLD",
				"posting_date": date(2026, 6, 1),
				"down_payment_amount": 100,
				"sales_order": "SO-OLD",
			}
		)

		with patch(
			"erpnext_anzahlungsrechnung.scripts.sales_invoice.get_prior_down_payment_invoices_for_final_invoice",
			return_value=[dpi],
		):
			rows = get_final_invoice_prior_down_payments(doc)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["invoice_no"], "AZ-OLD")
		self.assertTrue(rows[0]["show_invoice_details"])
