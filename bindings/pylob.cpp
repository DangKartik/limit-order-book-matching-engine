// Python bindings for the order book (module name: pylob).
//
// This file is the only place that knows about Python. The engine itself
// stays a plain C++ library with no Python dependency.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>   // automatic std::vector <-> list, std::optional <-> None, std::pair <-> tuple

#include <string>
#include <utility>
#include <vector>

#include "lob/order_book.hpp"

namespace py = pybind11;

namespace {

// What Python actually holds: a book plus the trade buffer the engine appends
// to. The engine's API takes a caller-owned buffer so that C++ callers can
// reuse it; here the wrapper is that caller, and one buffer lives as long as
// the book, so the C++ side still does no allocation per order.
template <typename Book>
struct PyBook {
    explicit PyBook(lob::BookConfig config) : book(config) {}
    Book book;
    std::vector<lob::Trade> buffer;
};

const char* side_name(lob::Side side) { return side == lob::Side::Buy ? "Side.Buy" : "Side.Sell"; }

// Registers one book implementation as a Python class. A function template, so
// the optimised and baseline books get identical Python interfaces.
template <typename Book>
void bind_book(py::module_& m, const char* name, const char* doc) {
    using Wrapper = PyBook<Book>;
    using Result = std::pair<lob::OrderResult, std::vector<lob::Trade>>;

    py::class_<Wrapper>(m, name, doc)
        // py::init with a lambda is a custom constructor. py::arg(...) = ...
        // gives the Python signature keyword arguments with defaults.
        .def(py::init([](lob::Price min_price, lob::Price max_price) {
                 // A std::invalid_argument thrown here reaches Python as ValueError.
                 return new Wrapper(lob::BookConfig{.min_price = min_price, .max_price = max_price});
             }),
             py::arg("min_price") = 1, py::arg("max_price") = 100'000)
        .def(
            "add_limit",
            [](Wrapper& self, lob::Side side, lob::Price price, lob::Quantity qty) -> Result {
                self.buffer.clear();
                const lob::OrderResult result = self.book.add_limit(side, price, qty, self.buffer);
                // Returning the vector by value copies the trades into a new
                // Python list. That copy is the cost of a Python-friendly API;
                // most orders produce no trades, so the list is usually empty.
                return {result, self.buffer};
            },
            py::arg("side"), py::arg("price"), py::arg("qty"),
            "Submit a limit order. Returns (OrderResult, list of Trade).")
        .def(
            "add_market",
            [](Wrapper& self, lob::Side side, lob::Quantity qty) -> Result {
                self.buffer.clear();
                const lob::OrderResult result = self.book.add_market(side, qty, self.buffer);
                return {result, self.buffer};
            },
            py::arg("side"), py::arg("qty"),
            "Submit a market order; any unfilled remainder is cancelled. "
            "Returns (OrderResult, list of Trade).")
        .def("cancel", [](Wrapper& self, lob::OrderId id) { return self.book.cancel(id); },
             py::arg("order_id"), "Cancel a resting order. Returns False if it is not resting.")
        .def("best_bid", [](const Wrapper& self) { return self.book.best_bid(); },
             "Highest bid price, or None if there are no bids.")
        .def("best_ask", [](const Wrapper& self) { return self.book.best_ask(); },
             "Lowest ask price, or None if there are no asks.")
        .def("quantity_at",
             [](const Wrapper& self, lob::Side side, lob::Price price) {
                 return self.book.quantity_at(side, price);
             },
             py::arg("side"), py::arg("price"))
        .def("depth",
             [](const Wrapper& self, lob::Side side, std::size_t max_levels) {
                 return self.book.depth(side, max_levels);
             },
             py::arg("side"), py::arg("max_levels") = 10,
             "Up to max_levels LevelView objects, best price first.")
        .def("order_count", [](const Wrapper& self) { return self.book.order_count(); })
        .def("__len__", [](const Wrapper& self) { return self.book.order_count(); })
        .def_property_readonly("min_price",
                               [](const Wrapper& self) { return self.book.config().min_price; })
        .def_property_readonly("max_price",
                               [](const Wrapper& self) { return self.book.config().max_price; });
}

}  // namespace

// This macro defines the function CPython calls when it runs `import pylob`.
// The first argument must match the file name of the built module.
PYBIND11_MODULE(pylob, m) {
    m.doc() = "Limit order book and matching engine (C++20), single instrument, single-threaded.";

    py::enum_<lob::Side>(m, "Side")
        .value("Buy", lob::Side::Buy)
        .value("Sell", lob::Side::Sell);

    py::enum_<lob::RejectReason>(m, "RejectReason")
        .value("None_", lob::RejectReason::None)   // `None` is a reserved word in Python
        .value("InvalidQuantity", lob::RejectReason::InvalidQuantity)
        .value("InvalidPrice", lob::RejectReason::InvalidPrice);

    // def_readonly exposes a C++ field as a read-only Python attribute.
    py::class_<lob::Trade>(m, "Trade")
        .def_readonly("maker_id", &lob::Trade::maker_id)
        .def_readonly("taker_id", &lob::Trade::taker_id)
        .def_readonly("price", &lob::Trade::price)
        .def_readonly("qty", &lob::Trade::qty)
        .def_readonly("taker_side", &lob::Trade::taker_side)
        .def("__eq__", [](const lob::Trade& a, const lob::Trade& b) { return a == b; })
        .def("__repr__", [](const lob::Trade& t) {
            return "Trade(maker_id=" + std::to_string(t.maker_id) +
                   ", taker_id=" + std::to_string(t.taker_id) +
                   ", price=" + std::to_string(t.price) + ", qty=" + std::to_string(t.qty) +
                   ", taker_side=" + side_name(t.taker_side) + ")";
        });

    py::class_<lob::OrderResult>(m, "OrderResult")
        .def_readonly("id", &lob::OrderResult::id)
        .def_readonly("filled", &lob::OrderResult::filled)
        .def_readonly("resting", &lob::OrderResult::resting)
        .def_readonly("reject", &lob::OrderResult::reject)
        .def_property_readonly(
            "accepted", [](const lob::OrderResult& r) { return r.reject == lob::RejectReason::None; })
        .def("__eq__", [](const lob::OrderResult& a, const lob::OrderResult& b) { return a == b; })
        .def("__repr__", [](const lob::OrderResult& r) {
            return "OrderResult(id=" + std::to_string(r.id) + ", filled=" + std::to_string(r.filled) +
                   ", resting=" + std::to_string(r.resting) + ")";
        });

    py::class_<lob::LevelView>(m, "LevelView")
        .def_readonly("price", &lob::LevelView::price)
        .def_readonly("qty", &lob::LevelView::qty)
        .def_readonly("order_count", &lob::LevelView::order_count)
        .def("__repr__", [](const lob::LevelView& l) {
            return "LevelView(price=" + std::to_string(l.price) + ", qty=" + std::to_string(l.qty) +
                   ", order_count=" + std::to_string(l.order_count) + ")";
        });

    bind_book<lob::FlatPoolBook>(
        m, "OrderBook", "The optimised book: flat array of price levels, pooled intrusive queues.");
    bind_book<lob::MapListBook>(
        m, "BaselineOrderBook", "The baseline book: std::map of price levels, std::list queues.");
}
