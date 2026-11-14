// The FIFO queue of orders resting at one price level, in two interchangeable
// implementations:
//
//   ListQueue       baseline      std::list<Order>, one heap allocation per order
//   IntrusiveQueue  optimisation  hand-written doubly linked list whose nodes
//                                 come from a reusable object pool
//
// Both expose the same member functions, so BasicOrderBook can be compiled
// against either one. There is no base class and no virtual function: the
// choice is a template argument, resolved at compile time. See DESIGN_NOTES #15.
#pragma once

#include <cstddef>
#include <iterator>
#include <list>
#include <memory>
#include <vector>

#include "lob/types.hpp"

namespace lob {

// A resting order as stored inside the book.
struct Order {
    OrderId id;
    Price price;
    Quantity qty;   // remaining (unfilled) quantity
    Side side;
};

// ---------------------------------------------------------------------------
// Baseline: std::list
// ---------------------------------------------------------------------------
class ListQueue {
public:
    // "Storage" is book-wide state shared by every queue. std::list allocates
    // its own nodes from the heap, so the baseline needs none; the empty struct
    // exists only so both queue types have the same function signatures.
    struct Storage {};

    // What the cancel index stores to find an order again in O(1).
    using Handle = std::list<Order>::iterator;

    Handle push_back(Storage&, const Order& order) {
        orders_.push_back(order);       // heap-allocates one list node
        return std::prev(orders_.end());
    }
    Order& front() { return orders_.front(); }
    void pop_front(Storage&) { orders_.pop_front(); }        // frees the node
    void erase(Storage&, Handle handle) { orders_.erase(handle); }

    [[nodiscard]] bool empty() const { return orders_.empty(); }
    [[nodiscard]] std::size_t size() const { return orders_.size(); }

    // Handle -> the order it refers to.
    static Order& order(Handle handle) { return *handle; }

private:
    std::list<Order> orders_;
};

// ---------------------------------------------------------------------------
// Optimisation: intrusive list over an object pool
// ---------------------------------------------------------------------------

// "Intrusive" means the prev/next links live inside the element itself, rather
// than in a separate node object that the container wraps around the element.
struct OrderNode {
    Order order;
    OrderNode* prev = nullptr;
    OrderNode* next = nullptr;   // doubles as the free-list link while the node is unused
};

// Hands out OrderNodes from large pre-allocated blocks and takes them back for
// reuse, instead of asking the operating system's allocator for every order.
// See DESIGN_NOTES #16.
class OrderPool {
public:
    explicit OrderPool(std::size_t chunk_size = 4096) : chunk_size_(chunk_size) {}

    OrderNode* allocate() {
        if (free_head_ == nullptr) {
            grow();
        }
        OrderNode* node = free_head_;
        free_head_ = node->next;   // pop from the free list
        ++live_;
        return node;
    }

    // Returns a node to the pool. The caller must not use `node` afterwards:
    // the next allocate() will hand out this same address.
    void release(OrderNode* node) {
        // Push to the FRONT of the free list (LIFO), so the node we just
        // touched, which is still in the CPU cache, is the next one reused.
        node->next = free_head_;
        free_head_ = node;
        --live_;
    }

    [[nodiscard]] std::size_t live() const { return live_; }
    [[nodiscard]] std::size_t capacity() const { return chunks_.size() * chunk_size_; }

private:
    void grow() {
        // std::unique_ptr<T[]> owns a heap array and delete[]s it when the
        // unique_ptr is destroyed. That is RAII: OrderPool needs no destructor,
        // and it is automatically non-copyable because unique_ptr is.
        //
        // The chunks vector may reallocate as it grows, but that only moves the
        // unique_ptrs (the owners), never the node arrays they point to. So an
        // OrderNode* stays valid for the pool's whole lifetime. A single
        // std::vector<OrderNode> could not promise that: growing it would move
        // every node and invalidate every pointer held by the book.
        chunks_.push_back(std::make_unique<OrderNode[]>(chunk_size_));
        OrderNode* chunk = chunks_.back().get();

        // Thread the new nodes onto the free list, in address order so that
        // consecutive allocations are adjacent in memory.
        for (std::size_t i = 0; i + 1 < chunk_size_; ++i) {
            chunk[i].next = &chunk[i + 1];
        }
        chunk[chunk_size_ - 1].next = free_head_;
        free_head_ = chunk;
    }

    std::vector<std::unique_ptr<OrderNode[]>> chunks_;
    OrderNode* free_head_ = nullptr;
    std::size_t chunk_size_;
    std::size_t live_ = 0;
};

class IntrusiveQueue {
public:
    using Storage = OrderPool;
    using Handle = OrderNode*;

    IntrusiveQueue() = default;
    // Copying would give two queues the same head/tail pointers into the same
    // nodes. Nothing needs a copy, so make it a compile error.
    IntrusiveQueue(const IntrusiveQueue&) = delete;
    IntrusiveQueue& operator=(const IntrusiveQueue&) = delete;

    Handle push_back(Storage& pool, const Order& order) {
        OrderNode* node = pool.allocate();
        node->order = order;
        node->next = nullptr;
        node->prev = tail_;
        if (tail_ != nullptr) {
            tail_->next = node;
        } else {
            head_ = node;   // the queue was empty
        }
        tail_ = node;
        ++size_;
        return node;
    }

    Order& front() { return head_->order; }
    void pop_front(Storage& pool) { erase(pool, head_); }

    // Unlink `node` from anywhere in the queue in O(1) and recycle it.
    void erase(Storage& pool, Handle node) {
        if (node->prev != nullptr) {
            node->prev->next = node->next;
        } else {
            head_ = node->next;   // node was the head
        }
        if (node->next != nullptr) {
            node->next->prev = node->prev;
        } else {
            tail_ = node->prev;   // node was the tail
        }
        --size_;
        pool.release(node);
    }

    [[nodiscard]] bool empty() const { return head_ == nullptr; }
    [[nodiscard]] std::size_t size() const { return size_; }

    static Order& order(Handle handle) { return handle->order; }

private:
    // The queue does not own its nodes; the pool does. That is why there is no
    // destructor here: when the book is destroyed, the pool frees every chunk.
    OrderNode* head_ = nullptr;
    OrderNode* tail_ = nullptr;
    std::size_t size_ = 0;
};

}  // namespace lob
